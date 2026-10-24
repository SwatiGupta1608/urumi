"""Runtime settings, read once from environment variables.

Credentials come from env vars populated by Kubernetes Secret references.
They are excluded from repr (field(repr=False)) and never logged or echoed
in error messages.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_KEY_RE = re.compile(r"^ck_[0-9a-f]{40}$")
_SECRET_RE = re.compile(r"^cs_[0-9a-f]{40}$")
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


class ConfigError(ValueError):
    """Invalid or missing configuration. Messages never contain secret values."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    consumer_key: str = field(repr=False)
    consumer_secret: str = field(repr=False)
    # Overrides TLS SNI + certificate hostname check + HTTP Host header, so the
    # server can dial Traefik's in-cluster Service while still validating the
    # certificate for the public store hostname.
    tls_server_name: str | None = None
    # CA bundle trusted for this upstream only. None = public CAs (certifi).
    ca_file: str | None = None
    connect_timeout: float = 5.0
    read_timeout: float = 15.0
    allowed_hosts: tuple[str, ...] = ("localhost:*", "127.0.0.1:*")
    allowed_origins: tuple[str, ...] = ("http://localhost:*", "http://127.0.0.1:*")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)

        def req(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigError(f"{name} is required")
            return value

        def opt(name: str) -> str | None:
            value = env.get(name, "").strip()
            return value or None

        def timeout(name: str, default: float) -> float:
            raw = opt(name)
            if raw is None:
                return default
            try:
                value = float(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be a number of seconds") from exc
            if not 0.5 <= value <= 60:
                raise ConfigError(f"{name} must be between 0.5 and 60 seconds")
            return value

        def csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw = opt(name)
            if raw is None:
                return default
            return tuple(item.strip() for item in raw.split(",") if item.strip())

        base_url = req("WC_BASE_URL").rstrip("/")
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname:
            # WooCommerce only accepts consumer-key Basic auth over HTTPS, and
            # we never send credentials over plain HTTP.
            raise ConfigError("WC_BASE_URL must be an https:// URL")
        if parts.path or parts.query or parts.fragment or parts.username:
            raise ConfigError("WC_BASE_URL must be scheme://host[:port] only")

        key = req("WC_CONSUMER_KEY")
        secret = req("WC_CONSUMER_SECRET")
        if not _KEY_RE.match(key):
            raise ConfigError("WC_CONSUMER_KEY has an unexpected format")
        if not _SECRET_RE.match(secret):
            raise ConfigError("WC_CONSUMER_SECRET has an unexpected format")

        server_name = opt("WC_TLS_SERVER_NAME")
        if server_name is not None and not _HOSTNAME_RE.match(server_name):
            raise ConfigError("WC_TLS_SERVER_NAME must be a DNS hostname")

        ca_file = opt("WC_CA_FILE")
        if ca_file is not None and not os.path.isfile(ca_file):
            raise ConfigError(f"WC_CA_FILE {ca_file} does not exist")

        return cls(
            base_url=base_url,
            consumer_key=key,
            consumer_secret=secret,
            tls_server_name=server_name,
            ca_file=ca_file,
            connect_timeout=timeout("WC_CONNECT_TIMEOUT", 5.0),
            read_timeout=timeout("WC_READ_TIMEOUT", 15.0),
            allowed_hosts=csv("MCP_ALLOWED_HOSTS", cls.allowed_hosts),
            allowed_origins=csv("MCP_ALLOWED_ORIGINS", cls.allowed_origins),
        )
