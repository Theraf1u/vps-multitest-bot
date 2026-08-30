from __future__ import annotations

import ipaddress
import re
import socket

FORBIDDEN_HOSTNAMES = {"localhost", "localhost.localdomain"}

# Cloud metadata endpoints (AWS/GCP/Azure/DO/...) all live on this single address.
METADATA_IP = ipaddress.ip_address("169.254.169.254")


class SSRFError(ValueError):
    pass


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip == METADATA_IP:
        return True
    return any(
        [
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_private,
            ip.is_reserved,
            ip.is_unspecified,
            getattr(ip, "is_site_local", False),
        ]
    )


def validate_target(host: str) -> str:
    """Resolves `host` and rejects it if it (or any resolved address) points at a
    non-public / internal network. Returns the host unchanged if it passes.

    Must be called again on the IP actually used to connect (DNS can rebind between
    the check and the connection), so callers should also validate the address
    returned by the SSH client at connect time.
    """
    host = host.strip().strip("[]")
    if not host:
        raise SSRFError("Пустой адрес сервера.")
    if host.lower() in FORBIDDEN_HOSTNAMES:
        raise SSRFError("Локальные адреса запрещены.")

    try:
        ip = ipaddress.ip_address(host)
        if _is_forbidden_ip(ip):
            raise SSRFError("Адрес указывает на локальную/служебную сеть.")
        return host
    except ValueError:
        pass  # not a literal IP, needs DNS resolution

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise SSRFError(f"Не удалось определить IP для {host}: {e}") from e

    if not infos:
        raise SSRFError(f"Не удалось определить IP для {host}.")

    for info in infos:
        addr = info[4][0]
        ip = ipaddress.ip_address(addr.split("%")[0])
        if _is_forbidden_ip(ip):
            raise SSRFError(f"{host} резолвится в запрещённый адрес ({ip}).")

    return host


def validate_connected_ip(ip_str: str) -> None:
    """Second check, run against the peer address the socket actually connected to."""
    ip = ipaddress.ip_address(ip_str.split("%")[0])
    if _is_forbidden_ip(ip):
        raise SSRFError(f"Соединение указывает на запрещённый адрес ({ip}).")


_PORT_RE = re.compile(r"^\d{1,5}$")


def validate_port(raw: str) -> int:
    raw = raw.strip()
    if not _PORT_RE.match(raw):
        raise ValueError("Порт должен быть числом.")
    port = int(raw)
    if not (1 <= port <= 65535):
        raise ValueError("Порт должен быть от 1 до 65535.")
    return port


_SECRET_PATTERNS = [
    re.compile(r"sk-or-v1-[A-Za-z0-9]+"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),  # telegram bot token shape
]


def redact(text: str) -> str:
    """Best-effort scrub of anything that looks like a secret, for logs/errors surfaced to users."""
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[redacted]", text)
    return text


def mask_key(key: str, keep: int = 4) -> str:
    """e.g. sk-or-v1-89b6...ddf1eb7 -> sk-or-v1-••••••••7Fd2"""
    if not key:
        return ""
    prefix = key[:8] if len(key) > 12 else ""
    tail = key[-keep:] if len(key) > keep else key
    return f"{prefix}{'•' * 8}{tail}"
