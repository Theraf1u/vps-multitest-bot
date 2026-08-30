from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil

from bot.config import settings

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(s: str) -> str:
    return _SAFE_RE.sub("_", s)[:80]


def report_filename(host: str, date_str: str) -> str:
    """User-facing PDF filename wherever a report is sent (right after a test, from the
    archive, from admin) — `<host>_<YYYY-MM-DD>.pdf`, sanitized for the filesystem."""
    return f"{_safe(host)}_{date_str}.pdf"


def delete_user_dir(user_id: int) -> None:
    """Permanently removes this user's entire report archive."""
    d = os.path.join(settings.archive_dir, str(user_id))
    shutil.rmtree(d, ignore_errors=True)


def user_dir(user_id: int) -> str:
    d = os.path.join(settings.archive_dir, str(user_id))
    os.makedirs(d, exist_ok=True)
    return d


def _reports_dir(user_id: int) -> str:
    d = os.path.join(user_dir(user_id), "reports")
    os.makedirs(d, exist_ok=True)
    return d


def report_basename(run_id: int, host: str) -> str:
    date_str = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return f"{run_id}_{date_str}_{_safe(host)}"


def save_report(
    user_id: int,
    run_id: int,
    host: str,
    pdf_source_path: str | None,
    raw_text: str,
    ai_text: str | None,
) -> dict:
    """Permanently archives one completed test's results (not subject to REPORT_TTL_HOURS,
    unlike REPORTS_DIR). Returns the paths actually written."""
    base = report_basename(run_id, host)
    out_dir = _reports_dir(user_id)
    written: dict = {}

    raw_path = os.path.join(out_dir, f"{base}_raw.txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(raw_text)
    written["raw"] = raw_path

    if ai_text:
        ai_path = os.path.join(out_dir, f"{base}_ai.txt")
        with open(ai_path, "w", encoding="utf-8") as f:
            f.write(ai_text)
        written["ai"] = ai_path

    if pdf_source_path and os.path.isfile(pdf_source_path):
        pdf_path = os.path.join(out_dir, f"{base}.pdf")
        shutil.copyfile(pdf_source_path, pdf_path)
        written["pdf"] = pdf_path

    for p in written.values():
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass

    return written


def build_raw_text(server_label: str, facts, result) -> str:
    """Plain-text dump of every test's parsed metrics/services/raw-log excerpt, for the
    permanent per-user archive (separate from the polished PDF)."""
    lines = [
        f"Server: {server_label}",
        f"Hostname: {facts.hostname}",
        f"OS: {facts.os_name}  Arch: {facts.arch}",
        f"CPU: {facts.cpu_model} ({facts.cpu_cores} cores)  RAM: {facts.ram_mb} MB  Disk: {facts.disk_total}",
        f"Дата: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60,
    ]
    for t in result.tests:
        lines.append(f"\n### {t.label} [{'OK' if t.ok else 'ERROR'}]")
        for name, value, _kind in t.metrics:
            lines.append(f"  {name}: {value}")
        for _type, name, status, value in t.services:
            lines.append(f"  [service] {name}: {status} {value}")
        if t.raw_log.strip():
            lines.append("  --- raw log (first 40 lines) ---")
            for line in t.raw_log.splitlines()[:40]:
                if line.strip():
                    lines.append(f"  {line}")
    return "\n".join(lines)


def list_users() -> list[str]:
    if not os.path.isdir(settings.archive_dir):
        return []
    return sorted(
        (d for d in os.listdir(settings.archive_dir) if os.path.isdir(os.path.join(settings.archive_dir, d))),
        key=lambda x: (len(x), x),
    )


_SUFFIX_RE = re.compile(r"(_raw\.txt|_ai\.txt|_payload\.json|\.pdf)$")


def list_user_reports(user_id: int, limit: int = 20) -> list[str]:
    """Report basenames (one per test run) for this user, newest first."""
    d = _reports_dir(user_id)
    bases = {_SUFFIX_RE.sub("", f) for f in os.listdir(d)}
    return sorted(bases, reverse=True)[:limit]


def report_pdf_path(user_id: int, base: str) -> str | None:
    p = os.path.join(_reports_dir(user_id), f"{base}.pdf")
    return p if os.path.isfile(p) else None


def attach_ai_text(user_id: int, run_id: int, ai_text: str) -> str | None:
    """Adds/overwrites the `_ai.txt` file for an already-archived report — used when the AI
    analysis is requested later via the "🤖 Проанализировать" button, after the PDF/raw dump
    were already saved. Returns the path written, or None if the report itself isn't archived."""
    base = find_report_base(user_id, run_id)
    if not base:
        return None
    path = os.path.join(_reports_dir(user_id), f"{base}_ai.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(ai_text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def find_report_base(user_id: int, run_id: int) -> str | None:
    """Report basenames are `{run_id}_{date}_{host}` — look one up by its DB run id."""
    prefix = f"{run_id}_"
    for base in list_user_reports(user_id, limit=1000):
        if base.startswith(prefix):
            return base
    return None


def save_ai_payload(user_id: int, run_id: int, payload: dict) -> str | None:
    """Persists the compact JSON payload built for OpenRouter alongside the archived report,
    so a *later* on-demand analysis (e.g. from history, possibly after a bot restart) can
    reuse the exact same test data without needing the in-memory post-test offer state."""
    base = find_report_base(user_id, run_id)
    if not base:
        return None
    path = os.path.join(_reports_dir(user_id), f"{base}_payload.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_ai_payload(user_id: int, run_id: int) -> dict | None:
    base = find_report_base(user_id, run_id)
    if not base:
        return None
    path = os.path.join(_reports_dir(user_id), f"{base}_payload.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
