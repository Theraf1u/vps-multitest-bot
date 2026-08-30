from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val or ""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(default_factory=lambda: _env("BOT_TOKEN", required=True))
    admin_id: int = field(default_factory=lambda: int(_env("ADMIN_ID", required=True)))

    openrouter_model: str = field(default_factory=lambda: _env("OPENROUTER_MODEL", "openai/gpt-4o-mini"))
    openrouter_api_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY", ""))

    master_encryption_key: str = field(default_factory=lambda: _env("MASTER_ENCRYPTION_KEY", required=True))

    max_concurrent_tests: int = field(default_factory=lambda: int(_env("MAX_CONCURRENT_TESTS", "3")))
    max_tests_per_user: int = field(default_factory=lambda: int(_env("MAX_TESTS_PER_USER", "1")))

    db_path: str = field(default_factory=lambda: _env("DB_PATH", "/app/data/bot.db"))
    reports_dir: str = field(default_factory=lambda: _env("REPORTS_DIR", "/app/data/reports"))
    report_ttl_hours: int = field(default_factory=lambda: int(_env("REPORT_TTL_HOURS", "24")))
    # Permanent, per-user report archive (raw text + AI summary + PDF), kept indefinitely (not
    # subject to REPORT_TTL_HOURS). Never holds SSH credentials — see bot/services/archive.py.
    archive_dir: str = field(default_factory=lambda: _env("ARCHIVE_DIR", "/app/data/archive"))

    telegram_api_base: str = field(default_factory=lambda: _env("TELEGRAM_API_BASE", ""))
    telegram_proxy_url: str = field(default_factory=lambda: _env("TELEGRAM_PROXY_URL", ""))
    # Both default to TELEGRAM_PROXY_URL's value: this host's direct egress to most non-Docker-
    # Hub destinations is unreliable (TCP handshake succeeds, data transfer stalls — see
    # README), and that turned out to include openrouter.ai and arbitrary SSH targets, not
    # just apt/pip. Set explicitly to "" to force a direct connection instead.
    openrouter_proxy_url: str = field(default_factory=lambda: _env("OPENROUTER_PROXY_URL", os.environ.get("TELEGRAM_PROXY_URL", "")))
    ssh_proxy_url: str = field(default_factory=lambda: _env("SSH_PROXY_URL", os.environ.get("TELEGRAM_PROXY_URL", "")))

    vendor_multitest_path: str = field(default_factory=lambda: _env("VENDOR_MULTITEST_PATH", "/app/vendor/multitest.sh"))
    multitest_repo_raw_url: str = field(
        default_factory=lambda: _env(
            "MULTITEST_REPO_RAW_URL",
            "https://raw.githubusercontent.com/saveksme/multitest/master/multitest.sh",
        )
    )

    ssh_connect_timeout: int = field(default_factory=lambda: int(_env("SSH_CONNECT_TIMEOUT", "45")))
    test_overall_timeout: int = field(default_factory=lambda: int(_env("TEST_OVERALL_TIMEOUT", "3600")))


settings = Settings()
