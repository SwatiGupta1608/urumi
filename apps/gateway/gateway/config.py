"""Gateway settings, read once from environment variables (Secrets via secretKeyRef)."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from urllib.parse import quote


class ConfigError(ValueError):
    """Invalid configuration. Messages never include secret values."""


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    key_pepper: bytes = field(repr=False)
    # Directory of server-side upstream credentials (one file per reference),
    # mounted from a Kubernetes Secret. Values are never returned by the API.
    credentials_dir: str = "/etc/gateway/credentials"
    # SSRF policy for upstream URLs.
    upstream_allowed_hosts: tuple[str, ...] = ("*.svc.cluster.local",)
    upstream_allowed_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    upstream_allowed_ports: tuple[int, ...] = (80, 443, 8000, 8080, 8082)
    upstream_allow_http: bool = True
    # Timeouts (seconds).
    discovery_timeout: float = 5.0
    call_timeout: float = 30.0
    connect_timeout: float = 3.0
    discovery_interval: float = 60.0
    # MCP endpoint DNS-rebinding protection.
    allowed_hosts: tuple[str, ...] = ("localhost:*", "127.0.0.1:*")
    allowed_origins: tuple[str, ...] = ("http://localhost:*", "http://127.0.0.1:*")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)

        def opt(name: str) -> str | None:
            v = env.get(name, "").strip()
            return v or None

        def num(name: str, default: float, lo: float, hi: float) -> float:
            raw = opt(name)
            if raw is None:
                return default
            try:
                v = float(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be a number") from exc
            if not lo <= v <= hi:
                raise ConfigError(f"{name} must be between {lo:g} and {hi:g}")
            return v

        def csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw = opt(name)
            return default if raw is None else tuple(x.strip() for x in raw.split(",") if x.strip())

        url = opt("GATEWAY_DATABASE_URL")
        if url is None:
            host, name, user = opt("GATEWAY_DB_HOST"), opt("GATEWAY_DB_NAME"), opt("GATEWAY_DB_USER")
            password = env.get("GATEWAY_DB_PASSWORD", "")
            if not (host and name and user and password):
                raise ConfigError("set GATEWAY_DATABASE_URL or GATEWAY_DB_HOST/NAME/USER/PASSWORD")
            port = opt("GATEWAY_DB_PORT") or "5432"
            url = f"postgresql+asyncpg://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{quote(name, safe='')}"

        pepper = env.get("GATEWAY_KEY_PEPPER", "")
        if len(pepper) < 32:
            raise ConfigError("GATEWAY_KEY_PEPPER must be at least 32 characters")

        try:
            cidrs = tuple(ipaddress.ip_network(c, strict=False) for c in csv("GATEWAY_UPSTREAM_ALLOWED_CIDRS", ()))
        except ValueError as exc:
            raise ConfigError("GATEWAY_UPSTREAM_ALLOWED_CIDRS contains an invalid CIDR") from exc
        try:
            ports = tuple(int(p) for p in csv("GATEWAY_UPSTREAM_ALLOWED_PORTS", tuple(map(str, cls.upstream_allowed_ports))))
        except ValueError as exc:
            raise ConfigError("GATEWAY_UPSTREAM_ALLOWED_PORTS must be integers") from exc

        return cls(
            database_url=url,
            key_pepper=pepper.encode(),
            credentials_dir=opt("GATEWAY_CREDENTIALS_DIR") or cls.credentials_dir,
            upstream_allowed_hosts=csv("GATEWAY_UPSTREAM_ALLOWED_HOSTS", cls.upstream_allowed_hosts),
            upstream_allowed_cidrs=cidrs,
            upstream_allowed_ports=ports,
            upstream_allow_http=(opt("GATEWAY_UPSTREAM_ALLOW_HTTP") or "true").lower() == "true",
            discovery_timeout=num("GATEWAY_DISCOVERY_TIMEOUT", 5.0, 0.5, 60),
            call_timeout=num("GATEWAY_CALL_TIMEOUT", 30.0, 1, 300),
            connect_timeout=num("GATEWAY_CONNECT_TIMEOUT", 3.0, 0.5, 30),
            discovery_interval=num("GATEWAY_DISCOVERY_INTERVAL", 60.0, 5, 3600),
            allowed_hosts=csv("MCP_ALLOWED_HOSTS", cls.allowed_hosts),
            allowed_origins=csv("MCP_ALLOWED_ORIGINS", cls.allowed_origins),
        )
