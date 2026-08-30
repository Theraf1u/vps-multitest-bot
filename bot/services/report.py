from __future__ import annotations

import datetime as dt
import os

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

from bot.services.multitest import CATEGORIES, TEST_CATALOG, MultitestResult, ParsedTest
from bot.services.ssh import SystemFacts

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")

RAW_LOG_MAX_LINES = 12
RAW_LOG_MAX_CHARS = 1500


def _by_func(result: MultitestResult) -> dict[str, ParsedTest]:
    return {t.func: t for t in result.tests}


def _test_view(t: ParsedTest | None, label: str) -> dict:
    if t is None:
        return {"label": label, "ok": False, "ran": False, "metrics": [], "services": []}
    primary = [m for m in t.metrics if m[2] == "pri"]
    secondary = [m for m in t.metrics if m[2] != "pri"]
    return {
        "label": t.label,
        "ok": t.ok,
        "ran": True,
        "primary_metrics": primary,
        "metrics": secondary,
        "services": t.services,
    }


def _raw_excerpt(t: ParsedTest) -> str:
    lines = [l for l in t.raw_log.splitlines() if l.strip()]
    excerpt = "\n".join(lines[:RAW_LOG_MAX_LINES])
    if len(excerpt) > RAW_LOG_MAX_CHARS:
        excerpt = excerpt[:RAW_LOG_MAX_CHARS] + "…"
    return excerpt


def build_context(
    server_label: str,
    facts: SystemFacts,
    result: MultitestResult,
    ai_text: str | None,
) -> dict:
    by_func = _by_func(result)
    by_id = {t.id: t.label for t in TEST_CATALOG}

    groups_view = []
    for _key, title, ids in CATEGORIES:
        # "tests", not "items": a plain dict's `.items` resolves to the builtin method
        # before Jinja falls back to key lookup, so `group.items` in the template would
        # silently return `dict.items` instead of our list.
        tests_view = [_test_view(by_func.get(tid), by_id[tid]) for tid in ids if tid in by_id]
        groups_view.append({"title": title, "tests": tests_view})

    detailed = [_test_view(by_func.get(t.id), t.label) for t in TEST_CATALOG]

    raw_section = []
    for t in TEST_CATALOG:
        parsed = by_func.get(t.id)
        if parsed and parsed.raw_log.strip():
            raw_section.append({"label": t.label, "ok": parsed.ok, "excerpt": _raw_excerpt(parsed)})

    return {
        "server_label": server_label,
        "facts": facts,
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ok_count": result.ok_count,
        "total_count": len(TEST_CATALOG),
        "groups": groups_view,
        "detailed": detailed,
        "raw_section": raw_section,
        "ai_text": ai_text,
    }


def render_pdf(
    server_label: str,
    facts: SystemFacts,
    result: MultitestResult,
    ai_text: str | None,
    out_path: str,
) -> None:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("report.html")
    ctx = build_context(server_label, facts, result, ai_text)
    html_str = template.render(**ctx)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    HTML(string=html_str).write_pdf(out_path)
