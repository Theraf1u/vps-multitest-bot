from __future__ import annotations

from bot.services.multitest import CATEGORIES, TEST_CATALOG, category_numbering

# Short, post-friendly descriptions for the admin/user-facing "что тестируем" screen. Numbered
# 1..N in TEST_CATALOG order so a test's number stays stable regardless of which category
# headers it's grouped under below.
TEST_DESCRIPTIONS: dict[str, str] = {
    "run_ip_region": "Определяет, из какой страны видят IP сервера зарубежные сервисы (геолокация по IP).",
    "run_censorcheck_geoblock": "Проверяет гео-блокировки — доступность сервисов, ограниченных по региону.",
    "run_censorcheck_dpi": "Ищет DPI-блокировки (Discord, Instagram, Telegram и др.) по глубокому анализу пакетов.",
    "run_censorcheck_tlab": "Альтернативная проверка цензуры/блокировок от tlab.pw по популярным платформам.",
    "run_iperf3_ru": "Измеряет скорость канала до серверов в России (iPerf3).",
    "run_iperf3_tlab": "Измеряет скорость канала до международных точек tlab (iPerf3).",
    "run_yabs": "Комплексный эталонный бенчмарк CPU, диска и сети для VPS.",
    "run_ip_check_place": "Проверяет репутацию IP: детект прокси/VPN/дата-центра, ASN, чёрные списки.",
    "run_bench_sh": "Классический тест CPU, дискового I/O и сети (dd/ioping).",
    "run_ip_quality": "Расширенная оценка качества IP: риск-скор и фрод-детекты.",
    "run_sysbench_cpu": "Однопоточный тест производительности процессора (sysbench).",
    "extra_dpi_detector": "Специализированная проверка DPI-блокировок Telegram (через Docker).",
    "extra_bench_gig": "Скорость канала до узлов в России.",
    "extra_speed_tlab": "Ещё один спидтест до международных точек tlab.",
    "extra_instagram_audio": "Проверяет региональную блокировку звука в Instagram Reels/Stories.",
    "extra_ipregion_xyz": "Независимый второй чекер гео-локации IP.",
    "extra_region_restriction": "Проверяет доступ к стриминг-сервисам (Netflix, Disney+, Spotify и др.) по регионам.",
    "extra_rbl_check": "Проверяет IP по ~190 спам-базам (RBL/DNSBL) — важно для почтовых серверов.",
    "extra_netquality": "Анализирует качество BGP-маршрутов, пиринга и сетевых соседей провайдера.",
    "extra_udp_throttle": "Проверяет, режет ли провайдер UDP-трафик — критично для WireGuard/Hysteria2/QUIC.",
    "extra_sustained_load": "Длинный тест скорости (90 сек) — ловит throttling/оверселл, которые быстрые бенчмарки не замечают.",
    "extra_ipv6": "Проверяет наличие и работоспособность IPv6-подключения.",
    "extra_bufferbloat": "Измеряет рост пинга под насыщающей нагрузкой — важно для звонков и игр через VPN.",
    "extra_dns_hijack": "Сравнивает ответы DNS через системный резолвер, напрямую и через DoH — ловит перехват DNS.",
    "extra_cdn_ttfb": "Измеряет время до первого байта от крупных CDN (Cloudflare, Google, Netflix, YouTube).",
}


def build_test_info_text() -> str:
    by_id = {t.id: t for t in TEST_CATALOG}
    order = category_numbering()

    lines = ["📋 *Все тесты Multitest-бота*", ""]
    for _key, title, ids in CATEGORIES:
        lines.append(f"*{title}*")
        for tid in ids:
            t = by_id.get(tid)
            if not t:
                continue
            desc = TEST_DESCRIPTIONS.get(tid, "")
            lines.append(f"{order[tid]}. *{t.label}* — {desc}")
        lines.append("")
    return "\n".join(lines).strip()


def _fmt_duration(secs: int) -> str:
    if secs < 60:
        return f"{secs} сек"
    minutes = int(secs / 60 + 0.5)
    return f"~{max(1, minutes)} мин"


def build_menu_intro_text() -> str:
    """Main-menu greeting — swapped from generic marketing copy to a per-test time budget
    (grouped by category, like the test-info screen) so a user can see up front what a full
    run actually costs them before tapping "Проверить сервер"."""
    by_id = {t.id: t for t in TEST_CATALOG}
    numbering = category_numbering()
    total_secs = sum(t.estimated_secs for t in TEST_CATALOG)

    lines = [
        "🧪 *Мультитест сервера*",
        f"Полная проверка VPS: {len(TEST_CATALOG)} тестов, {_fmt_duration(total_secs)}.",
        "",
    ]
    for _key, title, ids in CATEGORIES:
        cat_secs = sum(by_id[tid].estimated_secs for tid in ids if tid in by_id)
        lines.append(f"*{title}* — {_fmt_duration(cat_secs)}")
        for tid in ids:
            t = by_id.get(tid)
            if not t:
                continue
            lines.append(f"{numbering[tid]}. {t.label} — {_fmt_duration(t.estimated_secs)}")
        lines.append("")
    lines.append("Нажмите кнопку ниже, чтобы начать.")
    return "\n".join(lines).strip()
