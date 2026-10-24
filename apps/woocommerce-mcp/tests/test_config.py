import ssl

import pytest

from woocommerce_mcp.client import build_ssl_context
from woocommerce_mcp.config import ConfigError, Settings

from .conftest import CK, CS


def env(**over):
    e = {"WC_BASE_URL": "https://traefik.kube-system.svc.cluster.local", "WC_CONSUMER_KEY": CK, "WC_CONSUMER_SECRET": CS}
    e.update(over)
    return {k: v for k, v in e.items() if v is not None}


def test_valid_env():
    s = Settings.from_env(env(WC_TLS_SERVER_NAME="shop.localtest.me", MCP_ALLOWED_HOSTS="a:*, b:8000"))
    assert s.tls_server_name == "shop.localtest.me"
    assert s.allowed_hosts == ("a:*", "b:8000")


@pytest.mark.parametrize("url", ["http://shop.localtest.me", "ftp://x", "https://", "https://x/path", "https://u:p@x"])
def test_base_url_must_be_plain_https_origin(url):
    with pytest.raises(ConfigError):
        Settings.from_env(env(WC_BASE_URL=url))


@pytest.mark.parametrize("name", ["WC_BASE_URL", "WC_CONSUMER_KEY", "WC_CONSUMER_SECRET"])
def test_required(name):
    with pytest.raises(ConfigError, match=name):
        Settings.from_env(env(**{name: None}))


@pytest.mark.parametrize("over", [{"WC_CONSUMER_KEY": "ck_short"}, {"WC_CONSUMER_SECRET": "nope"}])
def test_credential_format_errors_do_not_echo_value(over):
    with pytest.raises(ConfigError) as exc:
        Settings.from_env(env(**over))
    assert list(over.values())[0] not in str(exc.value)


@pytest.mark.parametrize("value", ["0", "61", "abc"])
def test_timeouts_are_bounded(value):
    with pytest.raises(ConfigError):
        Settings.from_env(env(WC_READ_TIMEOUT=value))


def test_ca_file_must_exist():
    with pytest.raises(ConfigError):
        Settings.from_env(env(WC_CA_FILE="/nonexistent/ca.crt"))


def test_repr_hides_credentials():
    s = Settings.from_env(env())
    assert CK not in repr(s) and CS not in repr(s)


def test_ssl_context_always_verifies(settings):
    ctx = build_ssl_context(settings)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
