from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import os
import re
import shutil
import tarfile
import time
import uuid
from typing import Awaitable, Callable

import asyncssh
import httpx

from bot.config import settings
from bot.services import ssh as sshsvc


@dataclasses.dataclass(frozen=True)
class TestDef:
    id: str
    label: str
    estimated_secs: int
    kind: str  # "vendor" (a function inside vendor/multitest.sh) | "shell" (a standalone command)
    payload: str  # vendor: the function name to call; shell: the command to run
    dep: str | None = None  # shell only: command/package to `check_and_install` first


# Fixed catalog, in run order. The first 11 are vendor/multitest.sh's own tests — matching its
# `all_funcs`/`all_names` order exactly. We don't drive them through the vendored script's own
# `run_all()` (its non-interactive fallback only supports "run everything", no partial
# selection) — instead we call the lower-level functions it uses internally
# (`capture_test`/`parse_test_output`/`install_deps_for`) directly, for just the selected
# subset. If the pinned script is ever updated to a version that renames/reorders these
# functions, the "vendor" entries below must be updated to match (see admin "🔄 Multitest" ->
# update pinned script).
#
# The remaining entries are standalone community benchmark/censorship scripts that are *not*
# part of saveksme/multitest — run directly as shell commands, same capture mechanism, just no
# vendor parser (their raw output goes straight into the report's "Raw data" section instead of
# structured metrics/services).
#   Estimates below are calibrated from a real, complete 19/19 run on 104.252.19.95
#   (2026-08-30, 15:27–17:12 UTC) — extracted per-test wall time from the "Script started on"
#   timestamp each `script`-wrapped test writes as the first line of its own log (i.e. the gap
#   between test N's and test N+1's timestamps = test N's real total time, dependency install
#   included). Set to roughly that measured value, rounded up a bit for safety margin — better
#   an ETA that finishes early than one stuck at 99%. NetQuality in particular measured ~30 min
#   on that run (heavy first-run installs: nexttrace, speedtest-cli, iperf3, mtr, ...) but that's
#   worst-case-ish, so it's set well below the raw measurement rather than matching it exactly.
#   One real sample per test — revisit if pacing keeps diverging once there's more than one.
#   The 6 "extra_*" tests added 2026-08-30 for the VPN-node-reseller audience (UDP throttling,
#   sustained load, IPv6, bufferbloat, DNS hijack, CDN peering) are calibrated the same way, from
#   a real end-to-end run of just that subset against a disposable test VPS via the bot's actual
#   MultitestRun pipeline (not a standalone shell test) — see the same session's notes.
TEST_CATALOG: list[TestDef] = [
    TestDef("run_ip_region", "IP Region", 180, "vendor", "run_ip_region"),
    TestDef("run_censorcheck_geoblock", "Censorcheck Geo", 210, "vendor", "run_censorcheck_geoblock"),
    TestDef("run_censorcheck_dpi", "Censorcheck DPI", 270, "vendor", "run_censorcheck_dpi"),
    TestDef("run_censorcheck_tlab", "Censorcheck tlab", 180, "vendor", "run_censorcheck_tlab"),
    TestDef("run_iperf3_ru", "iPerf3 RU", 270, "vendor", "run_iperf3_ru"),
    TestDef("run_iperf3_tlab", "iPerf3 tlab", 480, "vendor", "run_iperf3_tlab"),
    TestDef("run_yabs", "YABS", 1080, "vendor", "run_yabs"),
    TestDef("run_ip_check_place", "IP Check", 420, "vendor", "run_ip_check_place"),
    TestDef("run_bench_sh", "bench.sh", 450, "vendor", "run_bench_sh"),
    TestDef("run_ip_quality", "IPQuality", 420, "vendor", "run_ip_quality"),
    TestDef("run_sysbench_cpu", "CPU", 25, "vendor", "run_sysbench_cpu"),
    TestDef(
        "extra_dpi_detector", "DPI Detector (Telegram)", 135, "shell",
        'if command -v docker >/dev/null 2>&1; then '
        'docker run --rm --pull=always --network host ghcr.io/runnin4ik/dpi-detector:latest -t 12345 --batch; '
        'else echo "Docker не установлен на этом сервере — тест DPI Detector пропущен."; fi',
    ),
    TestDef("extra_bench_gig", "bench.gig.ovh (РФ)", 480, "shell", "wget -qO- bench.gig.ovh | bash", dep="wget"),
    TestDef("extra_speed_tlab", "speed.tlab.pw", 420, "shell", "wget -qO- speed.tlab.pw | bash", dep="wget"),
    TestDef(
        "extra_instagram_audio", "Instagram Audio Block", 45, "shell",
        "bash <(curl -L -s https://bench.openode.xyz/checker_inst.sh)", dep="curl",
    ),
    TestDef(
        "extra_ipregion_xyz", "IP Region (ipregion.xyz)", 150, "shell", "bash <(wget -qO- https://ipregion.xyz)",
        # ipregion.xyz serves the same Davoyan/ipregion script as run_ip_region above — its own
        # dependency check (jq/curl/util-linux) prompts on stdin if missing, hanging/failing
        # headless. jq is the one actually missing in practice; wget is the base fetch dep.
        dep="wget jq",
    ),
    TestDef(
        "extra_region_restriction", "Region Restriction (Streaming)", 360, "shell",
        # -R 66 = "All Platforms" (the same value the script itself defaults to on a bare
        # ENTER) — passing it explicitly skips the interactive region-select menu, which would
        # otherwise hang forever on our non-tty session. -E en forces English output.
        "bash <(curl -Ls https://raw.githubusercontent.com/lmc999/RegionRestrictionCheck/main/check.sh) -E en -R 66",
        dep="curl",
    ),
    TestDef(
        "extra_rbl_check", "RBL Blacklist Check", 135, "shell",
        # The script requires -i <ip> (no auto-detect) and only ships a git-clone install, so we
        # fetch the raw script directly and discover the VPS's own public IP as a pre-step.
        # `dig` (dnsutils/bind-utils/bind-tools/bind, depending on distro) isn't covered by the
        # vendor script's single-package check_and_install, so it's installed inline here.
        'ip=$(curl -s --max-time 10 https://ifconfig.me || curl -s --max-time 10 https://api.ipify.org); '
        'command -v dig >/dev/null 2>&1 || (apt-get install -y dnsutils || dnf install -y bind-utils || '
        'yum install -y bind-utils || apk add --quiet bind-tools || pacman -S --noconfirm bind) >/dev/null 2>&1; '
        'bash <(curl -Ls https://raw.githubusercontent.com/kuyint/rbl-check/main/rbl_check.sh) -i "$ip"',
        dep="curl",
    ),
    TestDef(
        "extra_netquality", "NetQuality (BGP/Route)", 720, "shell",
        # -y auto-confirms the script's own dependency-install prompt (it would otherwise block
        # on `read -p` forever). -S 67 skips sections 6 (speedtest) and 7 (iperf3) — those
        # duplicate run_yabs/run_bench_sh/run_iperf3_ru/run_iperf3_tlab already in our catalog —
        # keeping just BGP/route-quality/latency, which nothing else here covers.
        "bash <(curl -Ls https://raw.githubusercontent.com/xykt/NetQuality/main/net.sh) -E -y -S 67",
        dep="curl",
    ),
    # --- Added for the VPN-node-reseller audience: none of the above tests cover UDP/QUIC
    # behavior, sustained (post-warmup) throughput, IPv6, bufferbloat, or DNS integrity — all
    # live-verified against a real VPS before shipping (see session notes). Both iperf3-based
    # tests below deliberately use different primary public servers (iperf.he.net vs
    # ping.online.net) so selecting both in one run doesn't collide on the same server's
    # single-client-at-a-time slot; each still falls back to the other + retries once, since
    # "server busy" from a public iperf3 node is normal contention, not a VPS-side problem.
    TestDef(
        "extra_udp_throttle", "UDP Throttling (WireGuard/QUIC)", 45, "shell",
        """echo "Тест UDP-throttling (эмуляция WireGuard/Hysteria2/QUIC трафика, 50 Мбит/с, 20 сек)"
for s in iperf.he.net ping.online.net; do
  for attempt in 1 2; do
    echo "Пробую сервер: $s (попытка $attempt)"
    OUT=$(timeout 25 iperf3 -c "$s" -u -b 50M -t 20 -f m 2>&1)
    RC=$?
    if [ "$RC" -eq 0 ]; then
      echo "$OUT" | grep -E "sender|receiver"
      echo "ИТОГ: сервер $s. Receiver-строка = реально дошедший трафик (bitrate/jitter/loss). Большой Lost % или сильно заниженный bitrate против заявленных 50M = провайдер режет UDP."
      exit 0
    fi
    echo "$OUT" | tail -1
    sleep 5
  done
done
echo "ИТОГ: публичные iperf3-серверы сейчас заняты/недоступны — тест временно недоступен, попробуйте повторить проверку позже."
""",
        dep="iperf3",
    ),
    TestDef(
        "extra_sustained_load", "Sustained Load (throttling/оверселл)", 110, "shell",
        # 90s/15s-interval TCP run — cheap oversold VPS often throttle well after YABS/bench.sh's
        # quick burst-style measurements finish. Result deliberately flags that a late-run drop
        # can also be the public test server's own fair-use cap (verified live: iperf.he.net
        # stayed flat across a 90s run while ping.online.net cliffed hard after ~45s) — not
        # attributed to the VPS without that caveat.
        """echo "Тест устойчивости скорости (90 сек TCP, ловим throttling после прогрева канала)"
for s in ping.online.net iperf.he.net; do
  for attempt in 1 2; do
    echo "Пробую сервер: $s (попытка $attempt)"
    OUT=$(timeout 100 iperf3 -c "$s" -t 90 -i 15 -f m 2>&1)
    RC=$?
    if [ "$RC" -eq 0 ]; then
      echo "$OUT" | grep -E "^\\[  5\\]" | grep -v "ID\\]"
      FIRST=$(echo "$OUT" | grep -E "^\\[  5\\]   0.00-15" | grep -oE "[0-9.]+ Mbits/sec" | head -1)
      LAST=$(echo "$OUT" | grep -E "^\\[  5\\]  75.00-90" | grep -oE "[0-9.]+ Mbits/sec" | head -1)
      echo "ИТОГ: сервер $s. Первые 15 сек: ${FIRST:-н/д}, последние 15 сек: ${LAST:-н/д}. Резкое падение может быть как throttling/оверселлом канала сервера, так и собственным fair-use лимитом публичного iperf3-узла — для надёжного вывода сравните с другим прогоном/сервером."
      exit 0
    fi
    echo "$OUT" | tail -1
    sleep 5
  done
done
echo "ИТОГ: публичные iperf3-серверы сейчас заняты/недоступны — тест временно недоступен, попробуйте повторить проверку позже."
""",
        dep="iperf3",
    ),
    TestDef(
        "extra_ipv6", "IPv6 Connectivity", 20, "shell",
        """IP6=$(ip -6 addr show scope global 2>/dev/null | awk '/inet6/{print $2}' | head -1)
if [ -z "$IP6" ]; then
  echo "IPv6 не настроен на этом сервере (только IPv4)."
  echo "ИТОГ: IPv6 отсутствует — клиенты, которым нужен IPv6-выход, здесь его не получат."
else
  echo "IPv6-адрес: $IP6"
  echo "Пинг до 1.1.1.1 (Cloudflare) по IPv6:"
  ping -6 -c 3 -W 2 2606:4700:4700::1111 2>&1 | tail -3
  echo "Пинг до 8.8.8.8 (Google) по IPv6:"
  ping -6 -c 3 -W 2 2001:4860:4860::8888 2>&1 | tail -3
  echo "HTTP через IPv6:"
  curl -6 -s --max-time 6 -o /dev/null -w "ipv6.google.com: http=%{http_code} time=%{time_total}s\\n" https://ipv6.google.com/ 2>&1
  echo "ИТОГ: IPv6 настроен; если пинги и HTTP-запрос выше прошли успешно (0% packet loss, http=200) — IPv6-выход рабочий."
fi
""",
        dep="curl",
    ),
    TestDef(
        "extra_bufferbloat", "Bufferbloat (латентность под нагрузкой)", 45, "shell",
        # Baseline ping, then saturate both directions via Cloudflare's speed-test endpoints
        # while re-pinging — a fast server can still ruin calls/games if buffers bloat under load.
        """echo "Тест Bufferbloat (рост пинга/джиттера под насыщающей нагрузкой — важно для звонков/игр через VPN)"
echo "--- Пинг в покое (10 пакетов до 1.1.1.1) ---"
IDLE=$(ping -c 10 -i 0.2 -W 2 1.1.1.1 2>&1)
echo "$IDLE" | tail -2

curl -s -o /dev/null --max-time 25 "https://speed.cloudflare.com/__down?bytes=500000000" &
DLPID=$!
( dd if=/dev/zero bs=1M count=300 2>/dev/null | curl -s -o /dev/null --max-time 25 -T - "https://speed.cloudflare.com/__up" ) &
ULPID=$!
sleep 3

echo "--- Пинг под насыщающей нагрузкой (одновременно download+upload) ---"
LOAD=$(ping -c 10 -i 0.2 -W 2 1.1.1.1 2>&1)
echo "$LOAD" | tail -2

wait $DLPID 2>/dev/null
wait $ULPID 2>/dev/null

IDLE_AVG=$(echo "$IDLE" | grep -oE '= [0-9.]+/[0-9.]+/[0-9.]+/[0-9.]+' | awk -F/ '{print $2}')
LOAD_AVG=$(echo "$LOAD" | grep -oE '= [0-9.]+/[0-9.]+/[0-9.]+/[0-9.]+' | awk -F/ '{print $2}')
echo "ИТОГ: пинг в покое ~${IDLE_AVG:-н/д} мс, под нагрузкой ~${LOAD_AVG:-н/д} мс. Рост в разы = заметный bufferbloat (буферы канала переполняются, звонки/игры будут лагать под нагрузкой)."
""",
        dep="curl",
    ),
    TestDef(
        "extra_dns_hijack", "DNS Hijack/Leak Check", 25, "shell",
        # Resolves a domain with a fixed, well-known answer (one.one.one.one -> 1.1.1.1/1.0.0.1)
        # via three independent paths — system stub resolver, explicit plain UDP:53 to 1.1.1.1,
        # and encrypted DoH — and flags any disagreement as likely network-level interception.
        """command -v dig >/dev/null 2>&1 || (apt-get install -y dnsutils || dnf install -y bind-utils || \\
  yum install -y bind-utils || apk add --quiet bind-tools || pacman -S --noconfirm bind) >/dev/null 2>&1

echo "Тест перехвата DNS (сравнение системного резолвера, публичного DNS напрямую и DoH)"

get_ips() {
  echo "$1" | grep -oE '[0-9]{1,3}(\\.[0-9]{1,3}){3}' | sort -u | tr '\\n' ',' | sed 's/,$//'
}

DOMAIN=one.one.one.one

SYS=$(dig +short +time=3 +tries=1 "$DOMAIN" A 2>/dev/null)
SYS_IPS=$(get_ips "$SYS")
echo "Системный резолвер: ${SYS_IPS:-нет ответа}"

DIRECT=$(dig +short +time=3 +tries=1 @1.1.1.1 "$DOMAIN" A 2>/dev/null)
DIRECT_IPS=$(get_ips "$DIRECT")
echo "Напрямую к 1.1.1.1 (UDP:53): ${DIRECT_IPS:-нет ответа}"

DOH=$(curl -s --max-time 6 -H 'accept: application/dns-json' "https://cloudflare-dns.com/dns-query?name=${DOMAIN}&type=A" 2>/dev/null)
DOH_IPS=$(get_ips "$DOH")
echo "DoH (HTTPS, шифрованный): ${DOH_IPS:-нет ответа}"

if [ -z "$SYS_IPS" ] && [ -z "$DIRECT_IPS" ] && [ -z "$DOH_IPS" ]; then
  echo "ИТОГ: ни один метод не получил ответ — DNS/сеть недоступны для проверки."
elif [ "$SYS_IPS" = "$DIRECT_IPS" ] && [ "$DIRECT_IPS" = "$DOH_IPS" ]; then
  echo "ИТОГ: все три метода сошлись — признаков перехвата/подмены DNS не обнаружено."
else
  echo "ИТОГ: ⚠️ ответы РАЗЛИЧАЮТСЯ между методами — возможен перехват/подмена DNS на уровне сети/провайдера."
fi
""",
        dep="curl",
    ),
    TestDef(
        "extra_cdn_ttfb", "CDN Peering (TTFB)", 25, "shell",
        "echo \"Тест TTFB до крупных CDN/edge-сетей (насколько хорошо датацентр запирован с большими сетями)\"\n"
        "for pair in \"Cloudflare:https://www.cloudflare.com/\" \"Google:https://www.google.com/\" "
        "\"Netflix:https://www.netflix.com/\" \"YouTube:https://www.youtube.com/\"; do\n"
        "  name=\"${pair%%:*}\"\n"
        "  url=\"${pair#*:}\"\n"
        "  RES=$(curl -s -o /dev/null --max-time 8 -w \"connect=%{time_connect}s ttfb=%{time_starttransfer}s "
        "total=%{time_total}s http=%{http_code}\" \"$url\" 2>/dev/null)\n"
        "  if [ -z \"$RES\" ]; then\n"
        "    echo \"$name: недоступен (таймаут/ошибка)\"\n"
        "  else\n"
        "    echo \"$name: $RES\"\n"
        "  fi\n"
        "done\n"
        "echo \"ИТОГ: ttfb ниже ~0.15-0.3с для основных CDN — хороший пиринг датацентра; заметно выше или "
        "таймауты — CDN плохо доступны с этого хоста.\"\n",
        dep="curl",
    ),
]

# Single source of truth for grouping tests by topic — used both by the test-picker keyboard
# and by the PDF report's section layout, so the two never drift apart.
CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("performance", "⚡ Производительность", [
        "run_yabs", "run_sysbench_cpu", "run_bench_sh", "extra_bench_gig", "extra_speed_tlab",
        "extra_sustained_load",
    ]),
    ("network", "🌍 Сеть", [
        "run_ip_region", "extra_ipregion_xyz", "run_iperf3_ru", "run_iperf3_tlab", "extra_netquality",
        "extra_udp_throttle", "extra_ipv6", "extra_bufferbloat", "extra_cdn_ttfb",
    ]),
    ("ip_quality", "🔐 IP Quality", [
        "run_ip_quality", "run_ip_check_place", "extra_region_restriction", "extra_rbl_check",
    ]),
    ("censorship", "🛡 Censorship / DPI", [
        "run_censorcheck_geoblock", "run_censorcheck_dpi", "run_censorcheck_tlab",
        "extra_dpi_detector", "extra_instagram_audio", "extra_dns_hijack",
    ]),
]

REMOTE_TMP_PREFIX = "/tmp/.mtbot-"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class MultitestError(Exception):
    pass


# Reconnect schedule for a dropped SSH connection mid-run: a burst of quick attempts first
# (covers a blip), then — if those all failed — many longer-spaced retries (covers the target
# VPS rebooting, a longer network outage, etc.) before finally giving up and asking the user to
# retry manually. ~6 immediate attempts (~1 min) + 20 delayed attempts 90s apart (~30 min) — a
# user shouldn't sit through an hour-long test only to get a bare "cancelled" from one blip;
# see the local per-test result cache below (_cache_test_locally/load_cached_partial) for the
# other half of this: even if every one of these attempts fails, nothing already completed is
# lost, since it was already saved to our own disk as it finished.
RECONNECT_IMMEDIATE_ATTEMPTS = 6
RECONNECT_IMMEDIATE_DELAY = 10.0
RECONNECT_DELAYED_ATTEMPTS = 20
RECONNECT_DELAYED_WAIT = 90.0

# Per-test watchdog: if one single test has been "running" way longer than its own estimate,
# something on the target is stuck (a hung dependency install, a script waiting on input that'll
# never come, etc.) — auto-skip it (same mechanism as the "⏭ Скипнуть тест" button) rather than
# let one frozen test hold the whole run hostage forever. A flat ceiling would be unfair either
# way — 40 min is nothing for a 25-second CPU test to hang, but too tight for extra_sustained_load
# (its own internal server-retry logic can legitimately run ~7 min) — so each test gets its own
# ceiling scaled off `estimated_secs`, bounded by a floor (short tests still get a real leash) and
# a hard cap (nothing waits forever even if its estimate is huge). NetQuality's worst observed run
# (~29 min on a cold first-run installing nexttrace/speedtest-cli/iperf3/mtr) comfortably fits.
TEST_HANG_MULTIPLIER = 4.0
TEST_HANG_FLOOR = 300.0  # 5 минут
TEST_HANG_CEILING = 2400.0  # 40 минут


def _hang_timeout_for(t: "TestDef") -> float:
    return min(max(t.estimated_secs * TEST_HANG_MULTIPLIER, TEST_HANG_FLOOR), TEST_HANG_CEILING)

# A half-dead connection (packets black-holed by the network rather than a clean TCP reset)
# never raises OSError/asyncssh.Error on its own — asyncssh just waits forever for a reply that
# isn't coming. Every remote command/SFTP operation below is bounded by one of these so a stall
# gets treated as a dropped connection (-> _reconnect) instead of hanging the whole run forever
# with the progress bar frozen and nothing in the logs to explain why.
POLL_COMMAND_TIMEOUT = 30.0
CMD_TIMEOUT = 30.0
SFTP_TIMEOUT = 60.0

StatusCallback = Callable[[str], Awaitable[None]]


class MultitestConnectionLost(MultitestError):
    """Raised when the SSH connection to the target drops mid-run and every reconnect attempt
    (immediate + delayed) failed. Carries the live `MultitestRun` — the actual test process on
    the target keeps running independently (launched via `setsid nohup`), so nothing here is
    lost; a caller can retry later by calling `.resume(...)` on the same instance instead of
    starting the whole test sequence over."""

    def __init__(self, run: "MultitestRun") -> None:
        super().__init__("Соединение с сервером потеряно, переподключиться не удалось.")
        self.run = run


@dataclasses.dataclass
class ParsedTest:
    func: str
    label: str
    ok: bool
    metrics: list[tuple[str, str, str]] = dataclasses.field(default_factory=list)  # name, value, kind
    services: list[tuple[str, str, str, str]] = dataclasses.field(default_factory=list)  # type, name, status, value
    raw_log: str = ""
    duration_secs: float | None = None  # wall-clock time this test took — see MultitestRun.test_durations


@dataclasses.dataclass
class MultitestResult:
    tests: list[ParsedTest]
    cancelled: bool = False
    # True when the user cut the run short via "📊 Отчёт по готовым" — tests without data
    # simply weren't reached, same as any other not-run test as far as the report is concerned.
    partial: bool = False

    @property
    def ok_count(self) -> int:
        return sum(1 for t in self.tests if t.ok)

    @property
    def failed_count(self) -> int:
        return sum(1 for t in self.tests if not t.ok)


ProgressCallback = Callable[[int, int | None, float | None], Awaitable[None]]
# (completed_count, running_index|None, seconds_elapsed_on_running_test|None)


def catalog_subset(selected_ids: list[str] | None) -> list[TestDef]:
    """TEST_CATALOG filtered to `selected_ids` and kept in catalog order; pass None (or an
    empty/falsy list) to select every test."""
    wanted = set(selected_ids) if selected_ids else None
    return [t for t in TEST_CATALOG if wanted is None or t.id in wanted]


def category_numbering() -> dict[str, int]:
    """Sequential 1..N numbering in CATEGORIES display order (category by category, top to
    bottom) — anywhere a test list is shown numbered to the user (test picker, test-info
    screen) should use this instead of TEST_CATALOG's own order, so numbers read 1, 2, 3...
    within each category instead of jumping around (TEST_CATALOG order != category order)."""
    numbering: dict[str, int] = {}
    n = 0
    for _key, _title, ids in CATEGORIES:
        for tid in ids:
            n += 1
            numbering[tid] = n
    return numbering


def _vendor_path() -> str:
    return settings.vendor_multitest_path


def pinned_sha256() -> str:
    with open(_vendor_path(), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def pinned_version() -> str:
    try:
        with open(_vendor_path(), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r'SCRIPT_VERSION="([^"]+)"', line.strip())
                if m:
                    return m.group(1)
    except FileNotFoundError:
        pass
    return "unknown"


async def check_github_update() -> dict:
    """Fetches the current GitHub master copy and compares its hash to the pinned one.
    Never executes or installs it — that only happens via update_pinned_script()."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(settings.multitest_repo_raw_url)
        resp.raise_for_status()
        remote_bytes = resp.content
    remote_sha = hashlib.sha256(remote_bytes).hexdigest()
    local_sha = pinned_sha256()
    return {
        "remote_sha256": remote_sha,
        "local_sha256": local_sha,
        "has_update": remote_sha != local_sha,
        "remote_size": len(remote_bytes),
    }


async def update_pinned_script() -> str:
    """Admin-triggered: downloads GitHub master and overwrites the pinned vendor copy."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(settings.multitest_repo_raw_url)
        resp.raise_for_status()
        remote_bytes = resp.content
    if not remote_bytes.startswith(b"#!/bin/bash"):
        raise MultitestError("Скачанный файл не похож на bash-скрипт — обновление отменено.")
    if b"MULTITEST_TEST" not in remote_bytes:
        raise MultitestError("В скачанном файле не найден хук MULTITEST_TEST — обновление отменено.")
    with open(_vendor_path(), "wb") as f:
        f.write(remote_bytes)
    return hashlib.sha256(remote_bytes).hexdigest()


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _parse_metrics_file(raw: bytes) -> list[tuple[str, str, str]]:
    out = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line:
            continue
        parts = line.split("\x1f")
        parts += [""] * (3 - len(parts))
        out.append((parts[0], parts[1], parts[2]))
    return out


def _parse_services_file(raw: bytes) -> list[tuple[str, str, str, str]]:
    out = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line:
            continue
        parts = line.split("\x1f")
        parts += [""] * (6 - len(parts))
        _type, name, _brand, status, value, _extra = parts[:6]
        if _type == "sep":
            continue
        out.append((_type, name, status, value))
    return out


def build_runner_script(
    remote_vendor: str,
    summary_dir: str,
    subset: list[TestDef],
    cmd_path_by_test_id: dict[str, str],
) -> str:
    """Pure string-building for the remote runner script — kept free of any SSH/SFTP calls so
    it can be unit-tested without a live connection. `cmd_path_by_test_id` must have one entry
    per "shell"-kind test in `subset`, pointing at the remote file holding its command (see
    MultitestRun._upload_scripts for why the command isn't just embedded inline)."""
    vendor_ids = [t.payload for t in subset if t.kind == "vendor"]

    blocks = []
    for idx, t in enumerate(subset, start=1):
        log = f'"$SUMMARY_DIR/test-{idx}.log"'
        pidfile = f'"$SUMMARY_DIR/test-{idx}.pid"'
        # Backgrounded + waited (instead of run inline) so the bot can kill just this one test
        # by process group ("⏭ Скип теста") without touching the runner script itself — `set -m`
        # (job control) at the top of this script is what makes each backgrounded `(...)` its
        # own process group, so `$!` doubles as the group id `kill -TERM -$!` needs.
        run_and_wait = (
            f'&\nTESTPID=$!\necho $TESTPID > {pidfile}\nwait $TESTPID 2>/dev/null\nrm -f {pidfile}'
        )
        if t.kind == "vendor":
            blocks.append(
                f'log={log}\n'
                f'( capture_test "{t.payload}" "$log" ) {run_and_wait}\n'
                f'parse_test_output "{t.payload}" "$log" "$SUMMARY_DIR/{t.payload}.metrics" "$SUMMARY_DIR/{t.payload}.services"'
            )
        else:
            cmd_path = cmd_path_by_test_id[t.id]
            # `dep` may be several space-separated commands (e.g. "wget jq") — each gets its
            # own check_and_install call, same as the multi-dep vendor functions do inline.
            dep_line = "".join(f"check_and_install {d}\n" for d in t.dep.split()) if t.dep else ""
            blocks.append(
                f'log={log}\n'
                f'{dep_line}'
                f'( COLUMNS=200 script -q -c "stty cols 200 2>/dev/null; bash \'{cmd_path}\'" "$log" ) {run_and_wait}\n'
                f': > "$SUMMARY_DIR/{t.id}.metrics"\n'
                f': > "$SUMMARY_DIR/{t.id}.services"'
            )
    body = "\n\n".join(blocks)

    return f"""#!/bin/bash
set -m
export MULTITEST_TEST=1
source {remote_vendor}
render_and_upload_summary() {{ :; }}

SUMMARY_DIR="{summary_dir}"
mkdir -p "$SUMMARY_DIR"
detect_script_flavor

install_deps_for {" ".join(vendor_ids)}

{body}

echo "___MULTITEST_DONE___:$SUMMARY_DIR"
"""


class MultitestRun:
    """One in-flight (or just-finished) multitest execution against a single SSH connection."""

    def __init__(self, conn: asyncssh.SSHClientConnection, subset: list[TestDef], creds: sshsvc.Credentials):
        self.conn = conn
        self.subset = subset
        self.creds = creds  # kept only to reconnect if the connection drops mid-run
        self.run_id = uuid.uuid4().hex[:10]
        self.remote_vendor = f"{REMOTE_TMP_PREFIX}{self.run_id}-vendor.sh"
        self.remote_runner = f"{REMOTE_TMP_PREFIX}{self.run_id}-runner.sh"
        self.remote_out = f"{REMOTE_TMP_PREFIX}{self.run_id}-out.log"
        self.summary_dir = f"/tmp/multitest-summary-{self.run_id}"
        self._cmd_files: list[str] = []  # remote paths of per-test shell-command scripts
        self._launch_pid: int | None = None
        self._cancelled = False
        self._last_running_idx: int | None = None  # 0-based index of the test polling last saw as "running"
        self.skipped_indices: set[int] = set()  # 0-based indices skipped via skip_current()

        # Our own local backup of each test's result, written the moment polling first sees it
        # complete — independent of the SSH connection to the target surviving afterward. If
        # reconnecting is later exhausted entirely (target gone for good, credentials revoked,
        # whatever), this is what a report gets salvaged from instead of nothing. See
        # _cache_test_locally / load_cached_partial.
        self.local_cache_dir = os.path.join(settings.reports_dir, "_partial_cache", self.run_id)
        self._cached_indices: set[int] = set()  # 1-based test indices already saved to local_cache_dir

        # Per-test wall-clock timing (0-based, same indexing as `subset`) — see test_durations()
        # / total_duration() below. Kept on self (not local to _poll_progress) so a resume() after
        # a reconnect doesn't lose timing already recorded for tests that finished before the drop.
        self._test_started_at: list[float | None] = [None] * len(subset)
        self._test_finished_at: list[float | None] = [None] * len(subset)
        self._run_started_at: float | None = None

    async def _run(self, cmd: str, check: bool = False, timeout: float = CMD_TIMEOUT):
        """self.conn.run(...) bounded by a timeout — see the module-level comment above
        POLL_COMMAND_TIMEOUT for why this matters. Raises asyncio.TimeoutError just like a
        connection error would, so callers that already handle (OSError, asyncssh.Error) should
        catch that too."""
        return await asyncio.wait_for(self.conn.run(cmd, check=check), timeout=timeout)

    async def _upload_scripts(self) -> None:
        async with asyncio.timeout(SFTP_TIMEOUT):
            async with self.conn.start_sftp_client() as sftp:
                await sftp.put(_vendor_path(), self.remote_vendor)

                # Each "shell"-kind test's command goes into its own file rather than being
                # embedded inline in the runner script text — several of these commands contain
                # double quotes, `<()`, `|`, etc. and writing them as file bytes over SFTP
                # sidesteps any shell-quoting headaches entirely.
                for idx, t in enumerate(self.subset, start=1):
                    if t.kind != "shell":
                        continue
                    cmd_path = f"{REMOTE_TMP_PREFIX}{self.run_id}-cmd-{idx}.sh"
                    self._cmd_files.append(cmd_path)
                    async with sftp.open(cmd_path, "w") as f:
                        await f.write(t.payload + "\n")

        cmd_path_by_test_id = dict(zip((t.id for t in self.subset if t.kind == "shell"), self._cmd_files))
        runner = build_runner_script(self.remote_vendor, self.summary_dir, self.subset, cmd_path_by_test_id)

        async with asyncio.timeout(SFTP_TIMEOUT):
            async with self.conn.start_sftp_client() as sftp:
                async with sftp.open(self.remote_runner, "w") as f:
                    await f.write(runner)

    async def _launch(self) -> None:
        cmd = (
            f"setsid nohup bash {self.remote_runner} > {self.remote_out} 2>&1 < /dev/null & "
            f"echo LAUNCHED_PID:$!"
        )
        result = await self._run(cmd)
        m = re.search(r"LAUNCHED_PID:(\d+)", result.stdout or "")
        if not m:
            raise MultitestError("Не удалось запустить Multitest на сервере.")
        self._launch_pid = int(m.group(1))

    async def _reconnect(self, on_status: StatusCallback | None) -> bool:
        """Tries to re-establish self.conn after it dropped. Never touches the remote run
        itself — the actual test process survives independently via `setsid nohup`, so a
        successful reconnect just lets polling resume where it left off."""
        try:
            self.conn.close()
        except Exception:
            pass

        attempt = 0
        total_attempts = RECONNECT_IMMEDIATE_ATTEMPTS + RECONNECT_DELAYED_ATTEMPTS
        schedule = [0.0] * RECONNECT_IMMEDIATE_ATTEMPTS + [RECONNECT_DELAYED_WAIT] * RECONNECT_DELAYED_ATTEMPTS
        for i, wait_before in enumerate(schedule):
            if wait_before:
                if on_status:
                    minutes = max(1, round(wait_before / 60))
                    await on_status(
                        f"⏳ Не удалось переподключиться сразу ({attempt} попыт.). "
                        f"Произошёл сбой связи, провожу переподключение — следующая попытка через "
                        f"~{minutes} мин ({attempt}/{total_attempts}). Тест на сервере продолжает идти."
                    )
                await asyncio.sleep(wait_before)
            elif i > 0:
                await asyncio.sleep(RECONNECT_IMMEDIATE_DELAY)

            attempt += 1
            if on_status:
                await on_status(
                    f"⚠️ Связь с сервером прервалась. Переподключаюсь... (попытка {attempt}/{total_attempts})"
                )
            try:
                self.conn = await sshsvc.connect(self.creds, timeout=settings.ssh_connect_timeout)
            except Exception:
                continue
            if on_status:
                await on_status("✅ Переподключение успешно, продолжаю опрос...")
            return True

        return False

    async def _poll_progress(
        self, on_progress: ProgressCallback | None, poll_interval: float, on_status: StatusCallback | None = None
    ) -> None:
        total = len(self.subset)
        # First wall-clock moment each test index was observed as "running", so we can
        # estimate a sub-progress percentage against its estimated_secs purely from polling —
        # neither the vendored script nor the standalone community scripts have a hook for
        # reporting fractional progress themselves. Also doubles as per-test duration tracking
        # (self._test_started_at/_test_finished_at) for the final report — see test_durations().
        start_times = self._test_started_at
        if self._run_started_at is None:
            self._run_started_at = time.monotonic()
        checks_since_any_progress = 0
        cached_up_to = 0

        while True:
            if self._cancelled:
                return
            try:
                result = await self._run(
                    f"ls -1 {self.summary_dir}/*.metrics 2>/dev/null; echo ---; "
                    f"ls -1 {self.summary_dir}/test-*.log 2>/dev/null; echo ---; "
                    f"cat {self.remote_out} 2>/dev/null | grep -c ___MULTITEST_DONE___",
                    timeout=POLL_COMMAND_TIMEOUT,
                )
            except (OSError, asyncssh.Error, asyncio.TimeoutError):
                # A silently half-dead connection (network black-holing packets rather than
                # cleanly resetting) never raises OSError/asyncssh.Error on its own — the `run`
                # call just hangs forever with no exception, no progress update, and no chance
                # to ever reconnect. Bound it explicitly so a stall gets treated exactly like any
                # other dropped connection instead of freezing the progress bar permanently.
                if self._cancelled:
                    return
                if not await self._reconnect(on_status):
                    raise MultitestConnectionLost(self)
                continue  # re-poll immediately on the fresh connection
            out = result.stdout or ""
            sections = out.split("---\n")
            metrics_listed = [l.strip() for l in sections[0].splitlines() if l.strip()] if len(sections) > 0 else []
            logs_listed = [l.strip() for l in sections[1].splitlines() if l.strip()] if len(sections) > 1 else []
            done_count = 0
            if len(sections) > 2:
                try:
                    done_count = int(sections[2].strip() or "0")
                except ValueError:
                    done_count = 0

            completed_ids = {os.path.basename(p).rsplit(".metrics", 1)[0] for p in metrics_listed}
            completed = sum(1 for t in self.subset if (t.payload if t.kind == "vendor" else t.id) in completed_ids)
            running_idx = None
            if len(logs_listed) > completed:
                running_idx = completed  # 0-based index of the test currently in progress

            running_elapsed = None
            if running_idx is not None:
                if start_times[running_idx] is None:
                    start_times[running_idx] = time.monotonic()
                running_elapsed = time.monotonic() - start_times[running_idx]
            self._last_running_idx = running_idx

            if running_idx is not None and running_elapsed is not None and running_idx not in self.skipped_indices:
                hang_limit = _hang_timeout_for(self.subset[running_idx])
                if running_elapsed > hang_limit:
                    label = self.subset[running_idx].label
                    if on_status:
                        await on_status(
                            f"⏱ Тест «{label}» завис (идёт дольше {int(hang_limit // 60)} мин) — "
                            "пропускаю автоматически, продолжаю остальные."
                        )
                    await self.skip_current()

            if completed > cached_up_to:
                for finished_idx in range(cached_up_to + 1, completed + 1):
                    await self._cache_test_locally(finished_idx)
                    if self._test_finished_at[finished_idx - 1] is None:
                        self._test_finished_at[finished_idx - 1] = time.monotonic()
                cached_up_to = completed

            if on_progress:
                await on_progress(completed, running_idx, running_elapsed)

            if done_count > 0 or completed >= total:
                return

            # Bail out with a useful error if the launcher died before finishing anything —
            # e.g. it crashed during dependency installation on a fresh VPS.
            if completed == 0 and running_idx is None:
                checks_since_any_progress += 1
                if checks_since_any_progress >= 3:
                    check = await self._run(f"kill -0 {self._launch_pid} 2>/dev/null; echo $?")
                    if (check.stdout or "").strip() != "0":
                        tail = await self._run(f"cat {self.remote_out} 2>/dev/null | tail -40")
                        raise MultitestError(
                            "Multitest завершился, не начав тесты. Хвост вывода:\n"
                            f"{(tail.stdout or '').strip()[-800:]}"
                        )
            else:
                checks_since_any_progress = 0

            await asyncio.sleep(poll_interval)

    async def cancel(self) -> None:
        self._cancelled = True
        try:
            if self._launch_pid:
                await self._run(f"kill -TERM -{self._launch_pid} 2>/dev/null")
                await asyncio.sleep(1)
                await self._run(f"kill -KILL -{self._launch_pid} 2>/dev/null")
            await self._run(f"pkill -9 -f '{self.summary_dir}' 2>/dev/null")
        except (OSError, asyncssh.Error, asyncio.TimeoutError):
            pass  # connection already dead — nothing more we can do from here

    async def skip_current(self) -> bool:
        """"⏭ Скип теста": kills just the one test currently running (by process group — see
        build_runner_script) so the runner moves on to the next block on its own; the rest of
        the sequence is untouched. Returns False if nothing was running to skip (e.g. it
        finished naturally right as the button was pressed)."""
        idx = self._last_running_idx
        if idx is None:
            return False
        pidfile = f"{self.summary_dir}/test-{idx + 1}.pid"
        try:
            result = await self._run(f"cat {pidfile} 2>/dev/null")
            pid = (result.stdout or "").strip()
            if not pid:
                return False
            await self._run(f"kill -TERM -{pid} 2>/dev/null")
            await asyncio.sleep(1)
            await self._run(f"kill -KILL -{pid} 2>/dev/null")
            self.skipped_indices.add(idx)
            return True
        except (OSError, asyncssh.Error, asyncio.TimeoutError):
            return False

    async def _cache_test_locally(self, idx: int) -> None:
        """Copies one just-finished test's raw files (log/metrics/services) to our own disk —
        called from _poll_progress the moment it sees that test complete, so the data survives
        on our side regardless of whether the SSH connection to the target is ever seen again.
        Best-effort: a failure here (connection already flaky) just means this one test isn't
        backed up yet — the next poll tick that still sees it as "completed" retries it, since
        `idx` only gets added to _cached_indices on success."""
        if idx in self._cached_indices:
            return
        t = self.subset[idx - 1]
        key = t.payload if t.kind == "vendor" else t.id
        try:
            os.makedirs(self.local_cache_dir, exist_ok=True)
            async with asyncio.timeout(SFTP_TIMEOUT):
                async with self.conn.start_sftp_client() as sftp:
                    for remote_name, local_name in (
                        (f"test-{idx}.log", f"{idx}.log"),
                        (f"{key}.metrics", f"{idx}.metrics"),
                        (f"{key}.services", f"{idx}.services"),
                    ):
                        data = b""
                        try:
                            async with sftp.open(f"{self.summary_dir}/{remote_name}", "rb") as rf:
                                data = await rf.read()
                        except (OSError, asyncssh.SFTPError):
                            pass  # that particular file may not exist for this test kind — fine
                        with open(os.path.join(self.local_cache_dir, local_name), "wb") as lf:
                            lf.write(data)
            self._cached_indices.add(idx)
        except (OSError, asyncssh.Error, asyncio.TimeoutError):
            pass

    def load_cached_partial(self) -> list[ParsedTest]:
        """Best-effort reconstruction of results purely from our own local backup — usable even
        with zero live connection to the target. The ultimate fallback once reconnecting has
        been exhausted entirely (see RECONNECT_* above)."""
        results: list[ParsedTest] = []
        for idx, t in enumerate(self.subset, start=1):
            log_text, metrics_raw, services_raw = "", b"", b""
            log_path = os.path.join(self.local_cache_dir, f"{idx}.log")
            metrics_path = os.path.join(self.local_cache_dir, f"{idx}.metrics")
            services_path = os.path.join(self.local_cache_dir, f"{idx}.services")
            if os.path.isfile(log_path):
                with open(log_path, "rb") as f:
                    log_text = _strip_ansi(f.read().decode("utf-8", errors="replace"))
            if os.path.isfile(metrics_path):
                with open(metrics_path, "rb") as f:
                    metrics_raw = f.read()
            if os.path.isfile(services_path):
                with open(services_path, "rb") as f:
                    services_raw = f.read()
            results.append(
                ParsedTest(
                    func=t.id,
                    label=t.label,
                    ok=bool(log_text.strip()),
                    metrics=_parse_metrics_file(metrics_raw),
                    services=_parse_services_file(services_raw),
                    raw_log=log_text,
                )
            )
        for pt, secs in zip(results, self.test_durations()):
            pt.duration_secs = secs
        return results

    def _cleanup_local_cache(self) -> None:
        shutil.rmtree(self.local_cache_dir, ignore_errors=True)

    def test_durations(self) -> list[float | None]:
        """Best-effort wall-clock seconds each test in `subset` took, 0-based in subset order.
        A test that finished between two polls without ever being observed as "running" (fast
        test, slow poll_interval) has no recorded start — falls back to the previous test's
        finish time (or the overall run start, for test 0) as its start instead of leaving a
        gap. None only when we truly have no data for a slot (e.g. it never finished at all)."""
        durations: list[float | None] = []
        prev_end = self._run_started_at
        for i in range(len(self.subset)):
            start = self._test_started_at[i] if self._test_started_at[i] is not None else prev_end
            end = self._test_finished_at[i]
            if start is not None and end is not None:
                durations.append(max(0.0, end - start))
                prev_end = end
            else:
                durations.append(None)
        return durations

    def total_duration(self) -> float | None:
        """Wall-clock seconds from launch to the last test finishing, or None if we never got
        far enough to know either endpoint."""
        if self._run_started_at is None:
            return None
        finish_times = [t for t in self._test_finished_at if t is not None]
        if not finish_times:
            return None
        return max(finish_times) - self._run_started_at

    async def _download_and_parse(self) -> list[ParsedTest]:
        remote_tar = f"{REMOTE_TMP_PREFIX}{self.run_id}-result.tar.gz"
        base = os.path.basename(self.summary_dir)
        await self._run(f"tar -C /tmp -czf {remote_tar} {base}", timeout=SFTP_TIMEOUT)

        buf = io.BytesIO()
        async with asyncio.timeout(SFTP_TIMEOUT):
            async with self.conn.start_sftp_client() as sftp:
                async with sftp.open(remote_tar, "rb") as rf:
                    buf.write(await rf.read())
        buf.seek(0)

        results: list[ParsedTest] = []
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            members = {m.name: m for m in tar.getmembers()}

            def read(name: str) -> bytes:
                path = f"{base}/{name}"
                if path not in members:
                    return b""
                f = tar.extractfile(members[path])
                return f.read() if f else b""

            for idx, t in enumerate(self.subset, start=1):
                key = t.payload if t.kind == "vendor" else t.id
                log_raw = read(f"test-{idx}.log")
                metrics_raw = read(f"{key}.metrics")
                services_raw = read(f"{key}.services")
                log_text = _strip_ansi(log_raw.decode("utf-8", errors="replace"))
                results.append(
                    ParsedTest(
                        func=t.id,
                        label=t.label,
                        ok=bool(log_text.strip()),
                        metrics=_parse_metrics_file(metrics_raw),
                        services=_parse_services_file(services_raw),
                        raw_log=log_text,
                    )
                )
        for pt, secs in zip(results, self.test_durations()):
            pt.duration_secs = secs
        await self._cleanup(remote_tar)
        return results

    async def _cleanup(self, remote_tar: str | None = None) -> None:
        paths = [self.remote_vendor, self.remote_runner, self.remote_out, self.summary_dir, *self._cmd_files]
        if remote_tar:
            paths.append(remote_tar)
        quoted = " ".join(f"'{p}'" for p in paths)
        try:
            await self._run(f"rm -rf {quoted}")
        except (OSError, asyncssh.Error, asyncio.TimeoutError):
            pass  # best-effort — the target's own /tmp will clean itself up eventually regardless

    async def abandon(self) -> None:
        """Called when the user gives up on a connection-lost run instead of retrying — best
        effort: reconnect once just long enough to kill the remote process and wipe its temp
        files, so nothing lingers on the target server forever. Swallows all errors — if the
        target is truly unreachable there's nothing more we can do from here, and the target's
        own /tmp will eventually clean itself up regardless."""
        self._cancelled = True
        self._cleanup_local_cache()
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self.conn = await sshsvc.connect(self.creds, timeout=settings.ssh_connect_timeout)
        except Exception:
            return
        try:
            if self._launch_pid:
                await self._run(f"kill -KILL -{self._launch_pid} 2>/dev/null")
            await self._run(f"pkill -9 -f '{self.summary_dir}' 2>/dev/null")
            await self._cleanup()
        except (OSError, asyncssh.Error, asyncio.TimeoutError):
            pass

    async def run(
        self,
        on_progress: ProgressCallback | None = None,
        poll_interval: float = 4.0,
        cancel_event: asyncio.Event | None = None,
        on_status: StatusCallback | None = None,
        finish_early_event: asyncio.Event | None = None,
    ) -> MultitestResult:
        await self._upload_scripts()
        await self._launch()
        return await self._poll_and_collect(on_progress, poll_interval, cancel_event, on_status, finish_early_event)

    async def resume(
        self,
        on_progress: ProgressCallback | None = None,
        poll_interval: float = 4.0,
        cancel_event: asyncio.Event | None = None,
        on_status: StatusCallback | None = None,
        finish_early_event: asyncio.Event | None = None,
    ) -> MultitestResult:
        """Picks a run back up after MultitestConnectionLost was given up on and the user asked
        to retry — does NOT re-upload or re-launch, so already-completed tests aren't redone;
        the remote process kept running on its own the whole time. Raises
        MultitestConnectionLost again if this manual attempt also can't reconnect."""
        if not await self._reconnect(on_status):
            raise MultitestConnectionLost(self)
        return await self._poll_and_collect(on_progress, poll_interval, cancel_event, on_status, finish_early_event)

    async def _finish_early(self) -> MultitestResult:
        """"📊 Отчёт по готовым": kills whatever test is currently running (if any) and the
        launcher itself, then builds a report from whichever tests already finished — the rest
        show up as "not run", same as any other test the user never selected."""
        if self._last_running_idx is not None:
            try:
                await self.skip_current()
            except (OSError, asyncssh.Error, asyncio.TimeoutError):
                pass
        try:
            if self._launch_pid:
                await self._run(f"kill -TERM -{self._launch_pid} 2>/dev/null")
                await asyncio.sleep(1)
                await self._run(f"kill -KILL -{self._launch_pid} 2>/dev/null")
            await self._run(f"pkill -9 -f '{self.summary_dir}' 2>/dev/null")
        except (OSError, asyncssh.Error, asyncio.TimeoutError):
            pass
        tests = await self._download_and_parse()
        return MultitestResult(tests=tests, cancelled=False, partial=True)

    async def _poll_and_collect(
        self,
        on_progress: ProgressCallback | None,
        poll_interval: float,
        cancel_event: asyncio.Event | None,
        on_status: StatusCallback | None,
        finish_early_event: asyncio.Event | None = None,
    ) -> MultitestResult:
        poll_task = asyncio.create_task(self._poll_progress(on_progress, poll_interval, on_status))
        triggers: dict[asyncio.Task, str] = {}
        if cancel_event is not None:
            triggers[asyncio.create_task(cancel_event.wait())] = "cancel"
        if finish_early_event is not None:
            triggers[asyncio.create_task(finish_early_event.wait())] = "finish_early"

        if triggers:
            done, _pending = await asyncio.wait(
                {poll_task, *triggers}, return_when=asyncio.FIRST_COMPLETED
            )
            winner = next((triggers[t] for t in triggers if t in done), None)
            for t in triggers:
                if t not in done:
                    t.cancel()

            if winner == "cancel" and not poll_task.done():
                await self.cancel()
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass
                await self._cleanup()
                self._cleanup_local_cache()
                return MultitestResult(tests=[], cancelled=True)

            if winner == "finish_early" and not poll_task.done():
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass
                result = await self._finish_early()
                self._cleanup_local_cache()
                return result

            await poll_task  # poll_task itself finished first — re-await to surface any exception
        else:
            await poll_task

        tests = await self._download_and_parse()
        self._cleanup_local_cache()
        return MultitestResult(tests=tests, cancelled=False)


async def run_multitest(
    conn: asyncssh.SSHClientConnection,
    subset: list[TestDef],
    creds: sshsvc.Credentials,
    on_progress: ProgressCallback | None = None,
    cancel_event: asyncio.Event | None = None,
    on_status: StatusCallback | None = None,
    finish_early_event: asyncio.Event | None = None,
) -> MultitestResult:
    run = MultitestRun(conn, subset, creds)
    return await run.run(
        on_progress=on_progress, cancel_event=cancel_event, on_status=on_status, finish_early_event=finish_early_event
    )
