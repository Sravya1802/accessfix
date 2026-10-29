"""SSRF guard: AccessFix only loads public http(s) sites, plus its own host (for the demo)."""
import asyncio
import ipaddress
import os
import socket
from functools import lru_cache
from urllib.parse import urlsplit

LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}


class UnsafeURL(ValueError):
    pass


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def own_origin() -> tuple[str, int]:
    u = urlsplit(os.getenv("PUBLIC_BASE_URL", "http://localhost:8000"))
    return (u.hostname or "").lower(), u.port or _default_port(u.scheme)


def is_own_host(host: str, port: int) -> bool:
    own_host, own_port = own_origin()
    host = host.lower()
    if port != own_port:
        return False
    if host == own_host:
        return True
    return host in LOCAL_NAMES and own_host in LOCAL_NAMES


def ip_blocked(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


@lru_cache(maxsize=1024)
def resolves_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    return bool(infos) and not any(ip_blocked(info[4][0]) for info in infos)


def host_allowed(scheme: str, host: str | None, port: int | None) -> bool:
    if scheme not in ("http", "https") or not host:
        return False
    if is_own_host(host, port or _default_port(scheme)):
        return True
    return resolves_public(host.lower())


async def url_allowed(url: str) -> bool:
    u = urlsplit(url)
    try:
        port = u.port
    except ValueError:
        return False
    return await asyncio.to_thread(host_allowed, u.scheme.lower(), u.hostname, port)


def validate_url(raw: str) -> str:
    """Normalize a user-entered URL and reject anything that isn't a public http(s) page."""
    url = (raw or "").strip()
    if not url:
        raise UnsafeURL("Enter a URL to scan.")
    if len(url) > 2048:
        raise UnsafeURL("That URL is too long.")
    if "://" not in url:
        url = "https://" + url
    u = urlsplit(url)
    scheme = u.scheme.lower()
    if scheme not in ("http", "https"):
        raise UnsafeURL("Only http and https URLs can be scanned.")
    try:
        port = u.port
    except ValueError:
        raise UnsafeURL("That URL has an invalid port.")
    if not u.hostname:
        raise UnsafeURL("That URL has no hostname.")
    if u.username or u.password:
        raise UnsafeURL("URLs with credentials aren't allowed.")
    if not host_allowed(scheme, u.hostname, port):
        raise UnsafeURL("That host is private, local, or doesn't resolve, so AccessFix won't load it.")
    return url
