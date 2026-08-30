from __future__ import annotations

import asyncio
import dataclasses
from urllib.parse import urlparse

import asyncssh

from bot.config import settings
from bot.services.security import SSRFError, validate_connected_ip, validate_target


@dataclasses.dataclass
class Credentials:
    """Holds SSH credentials only in memory for the lifetime of one test run.

    Never persisted to disk or logs. Call `wipe()` as soon as the run is done
    (success, failure, or cancel) so the password reference is dropped.
    """

    host: str
    port: int
    username: str
    password: str | None

    def wipe(self) -> None:
        self.password = None

    def __repr__(self) -> str:  # never let a stray log/print leak the password
        return f"Credentials(host={self.host!r}, port={self.port}, username={self.username!r}, password=***)"


class SSHConnectError(Exception):
    pass


class SSHAuthError(Exception):
    pass


class NotRootError(Exception):
    pass


@dataclasses.dataclass
class SystemFacts:
    hostname: str = "?"
    os_name: str = "?"
    arch: str = "?"
    cpu_model: str = "?"
    cpu_cores: str = "?"
    ram_mb: str = "?"
    disk_total: str = "?"


async def _open_socks_socket(host: str, port: int, timeout: float):
    """Connects to host:port through settings.ssh_proxy_url and returns a plain connected
    socket.socket, for use as asyncssh's/asyncio's `sock=` argument.

    Needed because this host's direct outbound TCP to some networks silently stalls after
    the handshake (see README) — the SOCKS proxy sing-box exposes for containers is the one
    path on this host that's confirmed to actually move data reliably.
    """
    from python_socks import ProxyType
    from python_socks.async_.asyncio import Proxy

    parsed = urlparse(settings.ssh_proxy_url)
    proxy = Proxy.create(
        proxy_type=ProxyType.SOCKS5,
        host=parsed.hostname,
        port=parsed.port,
    )
    return await proxy.connect(dest_host=host, dest_port=port, timeout=timeout)


async def check_tcp_reachable(host: str, port: int, timeout: float = 8.0) -> None:
    try:
        validate_target(host)
    except SSRFError as e:
        raise SSHConnectError(str(e)) from e

    try:
        if settings.ssh_proxy_url:
            sock = await asyncio.wait_for(_open_socks_socket(host, port, timeout), timeout=timeout)
            sock.close()
        else:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
    except (OSError, asyncio.TimeoutError) as e:
        raise SSHConnectError(f"Не удалось подключиться к {host}:{port} по TCP: {e}") from e


async def connect(creds: Credentials, timeout: float = 15.0) -> asyncssh.SSHClientConnection:
    try:
        validate_target(creds.host)
    except SSRFError as e:
        raise SSHConnectError(str(e)) from e

    try:
        if settings.ssh_proxy_url:
            sock = await _open_socks_socket(creds.host, creds.port, timeout)
            conn = await asyncssh.connect(
                sock=sock,
                username=creds.username,
                password=creds.password,
                known_hosts=None,  # we manage our own per-user fingerprint trust store
                client_keys=None,
                connect_timeout=timeout,
            )
        else:
            conn = await asyncssh.connect(
                creds.host,
                port=creds.port,
                username=creds.username,
                password=creds.password,
                known_hosts=None,
                client_keys=None,
                connect_timeout=timeout,
            )
    except asyncssh.PermissionDenied as e:
        raise SSHAuthError("Неверный логин или пароль.") from e
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
        raise SSHConnectError(f"Не удалось подключиться к {creds.host}:{creds.port}: {e}") from e

    # When tunneled through the SOCKS proxy, peername reflects the proxy hop, not the real
    # target — the pre-connect validate_target() above is the SSRF check that matters here.
    if not settings.ssh_proxy_url:
        peername = conn.get_extra_info("peername")
        if peername:
            try:
                validate_connected_ip(peername[0])
            except SSRFError as e:
                conn.close()
                raise SSHConnectError(str(e)) from e

    return conn


def fingerprint_of(conn: asyncssh.SSHClientConnection) -> str:
    key = conn.get_server_host_key()
    return key.get_fingerprint("sha256")


async def check_root(conn: asyncssh.SSHClientConnection) -> bool:
    result = await conn.run("id -u", check=False)
    if (result.stdout or "").strip() == "0":
        return True
    sudo_check = await conn.run("sudo -n true", check=False)
    return sudo_check.exit_status == 0


async def gather_system_facts(conn: asyncssh.SSHClientConnection) -> SystemFacts:
    script = r"""
echo "HOSTNAME=$(hostname 2>/dev/null)"
if [ -f /etc/os-release ]; then . /etc/os-release; echo "OS=$PRETTY_NAME"; else echo "OS=unknown"; fi
echo "ARCH=$(uname -m 2>/dev/null)"
echo "CORES=$(nproc 2>/dev/null)"
echo "RAM_MB=$(free -m 2>/dev/null | awk '/Mem:/{print $2}')"
echo "DISK=$(df -h / 2>/dev/null | awk 'NR==2{print $2}')"
echo "CPUMODEL=$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | sed 's/^ *//')"
"""
    result = await conn.run(script, check=False)
    facts = SystemFacts()
    for line in (result.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if key == "HOSTNAME" and val:
            facts.hostname = val
        elif key == "OS" and val:
            facts.os_name = val
        elif key == "ARCH" and val:
            facts.arch = val
        elif key == "CORES" and val:
            facts.cpu_cores = val
        elif key == "RAM_MB" and val:
            facts.ram_mb = val
        elif key == "DISK" and val:
            facts.disk_total = val
        elif key == "CPUMODEL" and val:
            facts.cpu_model = val
    return facts
