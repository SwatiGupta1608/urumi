"""Upstream URL validation (SSRF protection).

A URL is accepted only if
  * scheme is https (or http when allowed, for in-cluster plain-HTTP MCP),
  * there are no credentials, query or fragment, and the port is allowed,
  * the hostname matches an allowed pattern (default "*.svc.cluster.local"),
  * every address it resolves to is inside an allowed CIDR and is not
    loopback, link-local (incl. 169.254.169.254 metadata), multicast,
    unspecified or reserved.
Checked on registration and again before every upstream connection (DNS can
change). The NetworkPolicy egress allow-list remains the hard boundary.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
import socket
from urllib.parse import urlsplit

import anyio

from .config import Settings

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}$")
_METADATA_HOSTS = {"metadata", "metadata.google.internal", "instance-data", "metadata.azure.internal"}


class UnsafeURL(ValueError):
    pass


def _check_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, settings: Settings) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        raise UnsafeURL(f"address {ip} is not an allowed upstream destination")
    if not any(ip in net for net in settings.upstream_allowed_cidrs):
        raise UnsafeURL(f"address {ip} is outside the allowed upstream networks")


def validate_url_syntax(url: str, settings: Settings) -> tuple[str, int]:
    """Static checks. Returns (hostname, port)."""
    if len(url) > 500:
        raise UnsafeURL("URL is too long")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL("URL is malformed") from exc
    if parts.scheme not in ("https", "http") or (parts.scheme == "http" and not settings.upstream_allow_http):
        raise UnsafeURL("URL scheme must be https" + (" or http" if settings.upstream_allow_http else ""))
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeURL("URL must not contain credentials; use a credential reference")
    if parts.query or parts.fragment:
        raise UnsafeURL("URL must not contain a query or fragment")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURL("URL has no host")
    if not _PATH_RE.match(parts.path or "/"):
        raise UnsafeURL("URL path contains unsupported characters")
    port = port or (443 if parts.scheme == "https" else 80)
    if port not in settings.upstream_allowed_ports:
        raise UnsafeURL(f"port {port} is not an allowed upstream port")

    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        # IP literals bypass hostname policy, so they must still fall in an allowed CIDR.
        _check_ip(ip, settings)
        return host, port
    if not _HOST_RE.match(host) or host in _METADATA_HOSTS or host == "localhost" or host.endswith(".localhost"):
        raise UnsafeURL("hostname is not allowed")
    if not any(fnmatch.fnmatchcase(host, pattern) for pattern in settings.upstream_allowed_hosts):
        raise UnsafeURL("hostname is not in the allowed upstream host list")
    return host, port


async def validate_url(url: str, settings: Settings, *, resolve: bool = True) -> None:
    host, port = validate_url_syntax(url, settings)
    if not resolve:
        return
    try:
        ipaddress.ip_address(host.strip("[]"))
        return  # already checked
    except ValueError:
        pass
    try:
        infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeURL(f"hostname {host} does not resolve") from exc
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise UnsafeURL(f"hostname {host} does not resolve")
    for addr in addrs:
        _check_ip(ipaddress.ip_address(addr.split("%")[0]), settings)
