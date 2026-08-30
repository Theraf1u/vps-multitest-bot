from __future__ import annotations

import dataclasses
import json
import logging

import httpx

from bot.services.multitest import MultitestResult
from bot.services.ssh import SystemFacts

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = (
    "Ты — технический ассистент, который анализирует результаты диагностики VPS-сервера "
    "(Multitest). Тебе присылают компактный JSON: {\"server\": {...железо...}, "
    "\"tests\": {\"Название теста\": {\"status\", \"metrics\", \"services\"}}, \"errors\": [...]}.\n\n"
    "ГЛАВНОЕ: фокус на том, ЧТО ПОКАЗАЛ КАЖДЫЙ ПРОВЕДЁННЫЙ ТЕСТ, а не на характеристиках "
    "железа — про CPU/RAM/диск напиши одну короткую строку в начале и не возвращайся к ней.\n\n"
    "Правила:\n"
    "1. Ничего не придумывай и не додумывай — используй только то, что есть в JSON.\n"
    "2. Пройдись по каждому тесту из \"tests\" отдельным пунктом/разделом — не пропускай "
    "ни один. Если у теста status=\"error\" или данных нет — так и пиши «нет данных», "
    "не выдумывай значения вместо них.\n"
    "3. Для тестов на цензуру/DPI (Censorcheck любого режима, DPI Detector, Instagram Audio "
    "Block) — обязательно перечисляй КОНКРЕТНЫЕ домены/сервисы из services по имени "
    "(YouTube, Instagram, Telegram, Discord и т.д.), кто доступен, а кто заблокирован и как "
    "именно (DNS/TCP/TLS/DPI) — не ограничивайся общим «есть проблемы».\n"
    "4. Для сетевых тестов (iPerf3 RU/tlab, bench.sh, bench.gig.ovh, speed.tlab.pw) — приводи "
    "конкретные цифры скорости (Mbps) и задержки (ms) по направлениям/городам, если они есть "
    "в metrics.\n"
    "5. Для IP Region/IP Quality/IP Check — укажи, какие конкретно зарубежные сервисы (Netflix, "
    "Spotify, Steam и т.п.) недоступны или детектят прокси/датацентр/VPN.\n"
    "6. Объясняй простым языком, без сложного жаргона, но не жертвуй конкретными цифрами и "
    "именами сервисов ради краткости — это главная ценность отчёта.\n"
    "7. В конце — общий вывод на 2-3 предложения: для каких задач сервер подходит хорошо, "
    "а для каких плохо, с учётом именно результатов тестов, а не заявленных характеристик.\n\n"
    "Формат: разделы по тестам/группам тестов с заголовком, внутри — строки с "
    "эмодзи-индикатором в начале: 🟢 хорошо, 🟡 есть нюансы, 🔴 проблема. Пример:\n"
    "🖥 Сервер: 4 ядра, 4 GB RAM, NVMe 40 GB — достаточно для лёгкой нагрузки.\n\n"
    "🛡 Censorcheck DPI\n"
    "🟢 YouTube, Netflix, Twitch — доступны без ограничений\n"
    "🔴 Instagram, Discord — заблокированы (DPI/KEYWORD)\n\n"
    "🌍 iPerf3 RU\n"
    "🟡 До Москвы: 85 Mbps, 40 ms — работает, но не быстро\n\n"
    "Итог: сервер хорошо подходит для повседневных задач и доступа к большинству "
    "зарубежных сервисов, но не годится для трафика к заблокированным соцсетям без "
    "дополнительных инструментов обхода.\n\n"
    "Пиши подробно и по делу — ориентируйся на 350-550 слов, не сокращай в ущерб конкретике."
)

MAX_METRICS_PER_TEST = 25
# Censorcheck tests report ~28 domains each — keep the cap above that so the AI actually sees
# every domain by name instead of silently losing detail exactly where it matters most.
MAX_SERVICES_PER_TEST = 35
DEFAULT_MAX_TOKENS = 1600  # ~350-550 words in Russian; Cyrillic tokenizes heavier than English


def build_compact_payload(facts: SystemFacts, result: MultitestResult) -> dict:
    tests: dict = {}
    errors: list[str] = []

    for t in result.tests:
        entry: dict = {"status": "ok" if t.ok else "error"}
        if t.metrics:
            entry["metrics"] = {name: value for name, value, _kind in t.metrics[:MAX_METRICS_PER_TEST] if name}
        if t.services:
            entry["services"] = [
                {"name": name, "status": status, "value": value}
                for _type, name, status, value in t.services[:MAX_SERVICES_PER_TEST]
                if name
            ]
        tests[t.label] = entry
        if not t.ok:
            errors.append(t.label)

    return {
        "server": {
            "hostname": facts.hostname,
            "os": facts.os_name,
            "arch": facts.arch,
            "cpu_model": facts.cpu_model,
            "cpu_cores": facts.cpu_cores,
            "ram_mb": facts.ram_mb,
            "disk_total": facts.disk_total,
        },
        "tests": tests,
        "errors": errors,
    }


@dataclasses.dataclass
class AnalyzeResult:
    text: str
    model: str
    cost_usd: float
    tokens_in: int
    tokens_out: int


async def _call_once(
    api_key: str, model: str, payload: dict, proxy: str | None, max_tokens: int
) -> AnalyzeResult | None:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/saveksme/multitest",
        "X-Title": "VPS Multitest Bot",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        # Asks OpenRouter to report the actual $ cost of this call in the response, so we
        # don't have to maintain our own per-model pricing table.
        "usage": {"include": True},
    }

    client_kwargs: dict = {"timeout": 60}
    if proxy:
        client_kwargs["proxy"] = proxy

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(OPENROUTER_URL, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage") or {}
        return AnalyzeResult(
            text=text,
            model=data.get("model", model),
            cost_usd=float(usage.get("cost") or 0.0),
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
        )
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        log.warning("OpenRouter request failed (model=%s): %s", model, e)
        return None


async def analyze(
    api_key: str,
    model: str,
    payload: dict,
    proxy: str | None = None,
    fallback_model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> AnalyzeResult | None:
    """Tries `model` first, then `fallback_model` (if given and different) if that fails."""
    if not api_key:
        return None
    result = await _call_once(api_key, model, payload, proxy, max_tokens)
    if result is None and fallback_model and fallback_model != model:
        log.info("Primary model %s failed, retrying with fallback %s", model, fallback_model)
        result = await _call_once(api_key, fallback_model, payload, proxy, max_tokens)
    return result


async def check_api_key(api_key: str, model: str, proxy: str | None = None) -> tuple[bool, str]:
    if not api_key:
        return False, "Ключ не задан."
    headers = {"Authorization": f"Bearer {api_key}"}
    client_kwargs: dict = {"timeout": 20}
    if proxy:
        client_kwargs["proxy"] = proxy
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get("https://openrouter.ai/api/v1/key", headers=headers)
        if resp.status_code == 200:
            return True, "Ключ рабочий."
        return False, f"OpenRouter вернул статус {resp.status_code}."
    except httpx.HTTPError as e:
        return False, f"Ошибка соединения: {e}"
