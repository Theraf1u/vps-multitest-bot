from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import os
import re
import tarfile
import time
import uuid
from typing import Awaitable, Callable

import asyncssh
import httpx

from bot.config import settings


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
TEST_CATALOG: list[TestDef] = [
    TestDef("run_ip_region", "IP Region", 40, "vendor", "run_ip_region"),
    TestDef("run_censorcheck_geoblock", "Censorcheck Geo", 120, "vendor", "run_censorcheck_geoblock"),
    TestDef("run_censorcheck_dpi", "Censorcheck DPI", 180, "vendor", "run_censorcheck_dpi"),
    TestDef("run_censorcheck_tlab", "Censorcheck tlab", 120, "vendor", "run_censorcheck_tlab"),
    TestDef("run_iperf3_ru", "iPerf3 RU", 180, "vendor", "run_iperf3_ru"),
    TestDef("run_iperf3_tlab", "iPerf3 tlab", 120, "vendor", "run_iperf3_tlab"),
    TestDef("run_yabs", "YABS", 720, "vendor", "run_yabs"),
    TestDef("run_ip_check_place", "IP Check", 180, "vendor", "run_ip_check_place"),
    TestDef("run_bench_sh", "bench.sh", 300, "vendor", "run_bench_sh"),
    TestDef("run_ip_quality", "IPQuality", 180, "vendor", "run_ip_quality"),
    TestDef("run_sysbench_cpu", "CPU", 15, "vendor", "run_sysbench_cpu"),
    TestDef(
        "extra_dpi_detector", "DPI Detector (Telegram)", 90, "shell",
        'if command -v docker >/dev/null 2>&1; then '
        'docker run --rm --pull=always --network host ghcr.io/runnin4ik/dpi-detector:latest -t 12345 --batch; '
        'else echo "Docker не установлен на этом сервере — тест DPI Detector пропущен."; fi',
    ),
    TestDef("extra_bench_gig", "bench.gig.ovh (РФ)", 120, "shell", "wget -qO- bench.gig.ovh | bash", dep="wget"),
    TestDef("extra_speed_tlab", "speed.tlab.pw", 120, "shell", "wget -qO- speed.tlab.pw | bash", dep="wget"),
    TestDef(
        "extra_instagram_audio", "Instagram Audio Block", 30, "shell",
        "bash <(curl -L -s https://bench.openode.xyz/checker_inst.sh)", dep="curl",
    ),
    TestDef("extra_ipregion_xyz", "IP Region (ipregion.xyz)", 40, "shell", "bash <(wget -qO- https://ipregion.xyz)", dep="wget"),
    TestDef(
        "extra_region_restriction", "Region Restriction (Streaming)", 240, "shell",
        # -R 66 = "All Platforms" (the same value the script itself defaults to on a bare
        # ENTER) — passing it explicitly skips the interactive region-select menu, which would
        # otherwise hang forever on our non-tty session. -E en forces English output.
        "bash <(curl -Ls https://raw.githubusercontent.com/lmc999/RegionRestrictionCheck/main/check.sh) -E en -R 66",
        dep="curl",
    ),
    TestDef(
        "extra_rbl_check", "RBL Blacklist Check", 90, "shell",
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
        "extra_netquality", "NetQuality (BGP/Route)", 150, "shell",
        # -y auto-confirms the script's own dependency-install prompt (it would otherwise block
        # on `read -p` forever). -S 67 skips sections 6 (speedtest) and 7 (iperf3) — those
        # duplicate run_yabs/run_bench_sh/run_iperf3_ru/run_iperf3_tlab already in our catalog —
        # keeping just BGP/route-quality/latency, which nothing else here covers.
        "bash <(curl -Ls https://raw.githubusercontent.com/xykt/NetQuality/main/net.sh) -E -y -S 67",
        dep="curl",
    ),
]

# Single source of truth for grouping tests by topic — used both by the test-picker keyboard
# and by the PDF report's section layout, so the two never drift apart.
CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("performance", "⚡ Производительность", [
        "run_yabs", "run_sysbench_cpu", "run_bench_sh", "extra_bench_gig", "extra_speed_tlab",
    ]),
    ("network", "🌍 Сеть", [
        "run_ip_region", "extra_ipregion_xyz", "run_iperf3_ru", "run_iperf3_tlab", "extra_netquality",
    ]),
    ("ip_quality", "🔐 IP Quality", [
        "run_ip_quality", "run_ip_check_place", "extra_region_restriction", "extra_rbl_check",
    ]),
    ("censorship", "🛡 Censorship / DPI", [
        "run_censorcheck_geoblock", "run_censorcheck_dpi", "run_censorcheck_tlab",
        "extra_dpi_detector", "extra_instagram_audio",
    ]),
]

REMOTE_TMP_PREFIX = "/tmp/.mtbot-"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class MultitestError(Exception):
    pass


@dataclasses.dataclass
class ParsedTest:
    func: str
    label: str
    ok: bool
    metrics: list[tuple[str, str, str]] = dataclasses.field(default_factory=list)  # name, value, kind
    services: list[tuple[str, str, str, str]] = dataclasses.field(default_factory=list)  # type, name, status, value
    raw_log: str = ""


@dataclasses.dataclass
class MultitestResult:
    tests: list[ParsedTest]
    cancelled: bool = False

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
        if t.kind == "vendor":
            blocks.append(
                f'log={log}\n'
                f'( capture_test "{t.payload}" "$log" )\n'
                f'parse_test_output "{t.payload}" "$log" "$SUMMARY_DIR/{t.payload}.metrics" "$SUMMARY_DIR/{t.payload}.services"'
            )
        else:
            cmd_path = cmd_path_by_test_id[t.id]
            dep_line = f'check_and_install {t.dep}\n' if t.dep else ""
            blocks.append(
                f'log={log}\n'
                f'{dep_line}'
                f'( COLUMNS=200 script -q -c "stty cols 200 2>/dev/null; bash \'{cmd_path}\'" "$log" )\n'
                f': > "$SUMMARY_DIR/{t.id}.metrics"\n'
                f': > "$SUMMARY_DIR/{t.id}.services"'
            )
    body = "\n\n".join(blocks)

    return f"""#!/bin/bash
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

    def __init__(self, conn: asyncssh.SSHClientConnection, subset: list[TestDef]):
        self.conn = conn
        self.subset = subset
        self.run_id = uuid.uuid4().hex[:10]
        self.remote_vendor = f"{REMOTE_TMP_PREFIX}{self.run_id}-vendor.sh"
        self.remote_runner = f"{REMOTE_TMP_PREFIX}{self.run_id}-runner.sh"
        self.remote_out = f"{REMOTE_TMP_PREFIX}{self.run_id}-out.log"
        self.summary_dir = f"/tmp/multitest-summary-{self.run_id}"
        self._cmd_files: list[str] = []  # remote paths of per-test shell-command scripts
        self._launch_pid: int | None = None
        self._cancelled = False

    async def _upload_scripts(self) -> None:
        async with self.conn.start_sftp_client() as sftp:
            await sftp.put(_vendor_path(), self.remote_vendor)

            # Each "shell"-kind test's command goes into its own file rather than being
            # embedded inline in the runner script text — several of these commands contain
            # double quotes, `<()`, `|`, etc. and writing them as file bytes over SFTP sidesteps
            # any shell-quoting headaches entirely.
            for idx, t in enumerate(self.subset, start=1):
                if t.kind != "shell":
                    continue
                cmd_path = f"{REMOTE_TMP_PREFIX}{self.run_id}-cmd-{idx}.sh"
                self._cmd_files.append(cmd_path)
                async with sftp.open(cmd_path, "w") as f:
                    await f.write(t.payload + "\n")

        cmd_path_by_test_id = dict(zip((t.id for t in self.subset if t.kind == "shell"), self._cmd_files))
        runner = build_runner_script(self.remote_vendor, self.summary_dir, self.subset, cmd_path_by_test_id)

        async with self.conn.start_sftp_client() as sftp:
            async with sftp.open(self.remote_runner, "w") as f:
                await f.write(runner)

    async def _launch(self) -> None:
        cmd = (
            f"setsid nohup bash {self.remote_runner} > {self.remote_out} 2>&1 < /dev/null & "
            f"echo LAUNCHED_PID:$!"
        )
        result = await self.conn.run(cmd, check=False)
        m = re.search(r"LAUNCHED_PID:(\d+)", result.stdout or "")
        if not m:
            raise MultitestError("Не удалось запустить Multitest на сервере.")
        self._launch_pid = int(m.group(1))

    async def _poll_progress(self, on_progress: ProgressCallback | None, poll_interval: float) -> None:
        total = len(self.subset)
        # First wall-clock moment each test index was observed as "running", so we can
        # estimate a sub-progress percentage against its estimated_secs purely from polling —
        # neither the vendored script nor the standalone community scripts have a hook for
        # reporting fractional progress themselves.
        start_times: list[float | None] = [None] * total
        checks_since_any_progress = 0

        while True:
            if self._cancelled:
                return
            result = await self.conn.run(
                f"ls -1 {self.summary_dir}/*.metrics 2>/dev/null; echo ---; "
                f"ls -1 {self.summary_dir}/test-*.log 2>/dev/null; echo ---; "
                f"cat {self.remote_out} 2>/dev/null | grep -c ___MULTITEST_DONE___",
                check=False,
            )
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

            if on_progress:
                await on_progress(completed, running_idx, running_elapsed)

            if done_count > 0 or completed >= total:
                return

            # Bail out with a useful error if the launcher died before finishing anything —
            # e.g. it crashed during dependency installation on a fresh VPS.
            if completed == 0 and running_idx is None:
                checks_since_any_progress += 1
                if checks_since_any_progress >= 3:
                    check = await self.conn.run(f"kill -0 {self._launch_pid} 2>/dev/null; echo $?", check=False)
                    if (check.stdout or "").strip() != "0":
                        tail = await self.conn.run(f"cat {self.remote_out} 2>/dev/null | tail -40", check=False)
                        raise MultitestError(
                            "Multitest завершился, не начав тесты. Хвост вывода:\n"
                            f"{(tail.stdout or '').strip()[-800:]}"
                        )
            else:
                checks_since_any_progress = 0

            await asyncio.sleep(poll_interval)

    async def cancel(self) -> None:
        self._cancelled = True
        if self._launch_pid:
            await self.conn.run(f"kill -TERM -{self._launch_pid} 2>/dev/null", check=False)
            await asyncio.sleep(1)
            await self.conn.run(f"kill -KILL -{self._launch_pid} 2>/dev/null", check=False)
        await self.conn.run(f"pkill -9 -f '{self.summary_dir}' 2>/dev/null", check=False)

    async def _download_and_parse(self) -> list[ParsedTest]:
        remote_tar = f"{REMOTE_TMP_PREFIX}{self.run_id}-result.tar.gz"
        base = os.path.basename(self.summary_dir)
        await self.conn.run(f"tar -C /tmp -czf {remote_tar} {base}", check=False)

        buf = io.BytesIO()
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
        await self._cleanup(remote_tar)
        return results

    async def _cleanup(self, remote_tar: str | None = None) -> None:
        paths = [self.remote_vendor, self.remote_runner, self.remote_out, self.summary_dir, *self._cmd_files]
        if remote_tar:
            paths.append(remote_tar)
        quoted = " ".join(f"'{p}'" for p in paths)
        await self.conn.run(f"rm -rf {quoted}", check=False)

    async def run(
        self,
        on_progress: ProgressCallback | None = None,
        poll_interval: float = 4.0,
        cancel_event: asyncio.Event | None = None,
    ) -> MultitestResult:
        await self._upload_scripts()
        await self._launch()

        poll_task = asyncio.create_task(self._poll_progress(on_progress, poll_interval))
        if cancel_event is not None:
            cancel_wait = asyncio.create_task(cancel_event.wait())
            done, pending = await asyncio.wait(
                {poll_task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_wait in done and not poll_task.done():
                await self.cancel()
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass
                await self._cleanup()
                return MultitestResult(tests=[], cancelled=True)
            cancel_wait.cancel()
            await poll_task  # already finished; re-await to surface any exception it raised
        else:
            await poll_task

        tests = await self._download_and_parse()
        return MultitestResult(tests=tests, cancelled=False)


async def run_multitest(
    conn: asyncssh.SSHClientConnection,
    subset: list[TestDef],
    on_progress: ProgressCallback | None = None,
    cancel_event: asyncio.Event | None = None,
) -> MultitestResult:
    run = MultitestRun(conn, subset)
    return await run.run(on_progress=on_progress, cancel_event=cancel_event)
