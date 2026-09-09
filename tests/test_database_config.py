"""External database configuration: connection strings, TLS and *_FILE secrets."""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest
from pydantic import ValidationError

from geometrikks.config.settings import DatabaseSettings


def _settings(**overrides) -> DatabaseSettings:
    """Construct from the environment only, like the app does at startup."""
    return DatabaseSettings(**overrides)


# ---------------------------------------------------------------------------
# Connection string
# ---------------------------------------------------------------------------


def test_connection_string_fills_every_component(monkeypatch):
    monkeypatch.setenv(
        "DB_CONNECTION_STRING", "postgresql://svc:s3cr3t@pg.example.com:6432/metrics"
    )
    settings = _settings()

    assert (settings.host, settings.port, settings.database) == ("pg.example.com", 6432, "metrics")
    assert settings.user == "svc"
    assert settings.password.get_secret_value() == "s3cr3t"
    assert settings.url == "postgresql+asyncpg://svc:s3cr3t@pg.example.com:6432/metrics"


def test_connection_string_decodes_percent_encoded_credentials(monkeypatch):
    """A password with reserved characters survives the round trip."""
    monkeypatch.setenv("DB_CONNECTION_STRING", "postgresql://us%40r:p%2Fw%3A1@db:5432/geo")
    settings = _settings()

    assert settings.user == "us@r"
    assert settings.password.get_secret_value() == "p/w:1"
    assert settings.url == "postgresql+asyncpg://us%40r:p%2Fw%3A1@db:5432/geo"


@pytest.mark.parametrize("variable", ["DB_CONNECTION_STRING", "DATABASE_URL"])
def test_both_connection_string_variables_are_accepted(monkeypatch, variable):
    monkeypatch.setenv(variable, "postgres://u:p@managed.example:5433/appdb")
    assert _settings().host == "managed.example"


def test_connection_string_wins_over_individual_variables(monkeypatch):
    """The string is one atomic address; an inherited DB_HOST must not split it."""
    monkeypatch.setenv("DB_HOST", "timescale_db")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_CONNECTION_STRING", "postgresql://u:p@external.example:6432/geo")
    settings = _settings()

    assert (settings.host, settings.port) == ("external.example", 6432)


def test_individual_variables_fill_what_the_string_omits(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "42")
    monkeypatch.setenv("DB_CONNECTION_STRING", "postgresql://u:p@external.example/geo")
    settings = _settings()

    assert settings.pool_size == 42
    assert settings.port == 5432  # default kept: the string carries no port


def test_libpq_query_parameters_configure_tls(monkeypatch):
    monkeypatch.setenv("DB_CONNECTION_STRING", "postgresql://u:p@db:5432/geo?sslmode=require")
    settings = _settings()

    assert settings.sslmode == "require"
    assert settings.connect_args == {"ssl": "require"}
    # TLS never travels in the URL: SQLAlchemy would hand it to asyncpg as an
    # unknown keyword argument.
    assert "sslmode" not in settings.url


def test_unknown_query_parameters_become_server_settings(monkeypatch):
    monkeypatch.setenv(
        "DB_CONNECTION_STRING", "postgresql://u:p@db:5432/geo?application_name=gm&options=-csearch_path%3Dpublic"
    )
    settings = _settings()

    assert settings.server_settings == {"application_name": "gm", "options": "-csearch_path=public"}
    assert settings.connect_args["server_settings"] == settings.server_settings


def test_explicit_server_settings_win_over_the_string(monkeypatch):
    monkeypatch.setenv("DB_SERVER_SETTINGS", '{"application_name": "explicit"}')
    monkeypatch.setenv("DB_CONNECTION_STRING", "postgresql://u:p@db:5432/geo?application_name=from-dsn&x=1")
    settings = _settings()

    assert settings.server_settings == {"application_name": "explicit", "x": "1"}


def test_connection_string_rejects_other_engines(monkeypatch):
    monkeypatch.setenv("DB_CONNECTION_STRING", "mysql://u:p@db:3306/geo")
    with pytest.raises(ValidationError, match="must start with postgresql"):
        _settings()


def test_connection_string_rejects_unix_sockets(monkeypatch):
    monkeypatch.setenv("DB_CONNECTION_STRING", "postgresql:///geo?host=/var/run/postgresql")
    with pytest.raises(ValidationError, match="Unix socket"):
        _settings()


def test_connection_string_rejects_unknown_sslmode(monkeypatch):
    monkeypatch.setenv("DB_CONNECTION_STRING", "postgresql://u:p@db/geo?sslmode=maybe")
    with pytest.raises(ValidationError, match="sslmode"):
        _settings()


def test_error_never_echoes_the_connection_string(monkeypatch):
    """The string carries the password; it must stay out of logs and tracebacks."""
    monkeypatch.setenv("DB_CONNECTION_STRING", "mysql://user:hunter2@db.example:3306/geo")
    with pytest.raises(ValidationError) as excinfo:
        _settings()

    assert "hunter2" not in str(excinfo.value)


def test_ipv6_host_is_bracketed(monkeypatch):
    monkeypatch.setenv("DB_HOST", "2001:db8::5")
    assert "@[2001:db8::5]:5432/" in _settings().url


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------


def test_no_tls_configuration_leaves_asyncpg_defaults():
    assert _settings().connect_args == {}
    assert _settings().ssl_argument is None


def test_sslmode_disable_turns_tls_off(monkeypatch):
    monkeypatch.setenv("DB_SSLMODE", "disable")
    assert _settings().ssl_argument is False


@pytest.mark.parametrize("mode", ["allow", "prefer", "require"])
def test_bare_modes_are_handed_to_asyncpg_verbatim(monkeypatch, mode):
    """asyncpg implements libpq's semantics for these; no context needed."""
    monkeypatch.setenv("DB_SSLMODE", mode)
    assert _settings().ssl_argument == mode


def test_verify_full_without_ca_file_uses_the_system_trust_store(monkeypatch):
    """Managed providers present publicly signed certificates; asyncpg alone
    would insist on ~/.postgresql/root.crt.

    Asserted through the call, not through get_ca_certs(): where OpenSSL
    loads the system store from a hashed directory it stays empty until a
    handshake looks a certificate up.
    """
    loaded: list[ssl.Purpose] = []
    monkeypatch.setattr(
        ssl.SSLContext,
        "load_default_certs",
        lambda self, purpose=ssl.Purpose.SERVER_AUTH: loaded.append(purpose),
    )
    monkeypatch.setenv("DB_SSLMODE", "verify-full")
    context = _settings().ssl_argument

    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert loaded == [ssl.Purpose.SERVER_AUTH]


def test_verify_ca_does_not_check_the_hostname(monkeypatch, ca_file: Path):
    monkeypatch.setenv("DB_SSLMODE", "verify-ca")
    monkeypatch.setenv("DB_SSLROOTCERT", str(ca_file))
    context = _settings().ssl_argument

    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is False
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert len(context.get_ca_certs()) == 1  # only the configured CA


def test_missing_certificate_file_fails_at_startup(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DB_SSLROOTCERT", str(tmp_path / "absent.pem"))
    with pytest.raises(ValidationError, match="certificate file not found"):
        _settings()


def test_client_key_without_certificate_is_refused(monkeypatch, ca_file: Path):
    monkeypatch.setenv("DB_SSLKEY", str(ca_file))
    with pytest.raises(ValidationError, match="DB_SSLKEY needs DB_SSLCERT"):
        _settings()


# ---------------------------------------------------------------------------
# *_FILE indirection
# ---------------------------------------------------------------------------


def test_any_variable_can_come_from_a_file(monkeypatch, tmp_path: Path):
    secret = tmp_path / "password"
    secret.write_text("from-secret-volume\n")  # trailing newline: the usual case
    monkeypatch.setenv("DB_PASSWORD_FILE", str(secret))

    assert _settings().password.get_secret_value() == "from-secret-volume"


def test_file_wins_over_the_plain_variable(monkeypatch, tmp_path: Path):
    secret = tmp_path / "password"
    secret.write_text("from-file")
    monkeypatch.setenv("DB_PASSWORD", "from-env")
    monkeypatch.setenv("DB_PASSWORD_FILE", str(secret))

    assert _settings().password.get_secret_value() == "from-file"


def test_connection_string_can_come_from_a_file(monkeypatch, tmp_path: Path):
    secret = tmp_path / "dsn"
    secret.write_text("postgresql://u:p@secret-host:5432/geo?sslmode=require\n")
    monkeypatch.setenv("DB_CONNECTION_STRING_FILE", str(secret))
    settings = _settings()

    assert settings.host == "secret-host"
    assert settings.sslmode == "require"


def test_unreadable_secret_file_fails_loudly(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DB_PASSWORD_FILE", str(tmp_path / "never-mounted"))
    with pytest.raises(Exception, match="DB_PASSWORD"):
        _settings()


def test_file_indirection_works_outside_the_database_section(monkeypatch, tmp_path: Path):
    from geometrikks.config.settings import Settings

    secret = tmp_path / "admin"
    secret.write_text("admin-password")
    monkeypatch.setenv("APP_ADMIN_PASSWORD_FILE", str(secret))

    password = Settings().admin_password
    assert password is not None
    assert password.get_secret_value() == "admin-password"


# ---------------------------------------------------------------------------
# The consumers
# ---------------------------------------------------------------------------


def test_engine_and_channels_backend_share_the_connection_parameters(monkeypatch):
    """Both talk to the same server, so both must carry the same TLS setup."""
    from functools import partial
    from unittest.mock import MagicMock

    from geometrikks.config.settings import Settings
    from geometrikks.server import plugins

    monkeypatch.setenv("DB_SSLMODE", "require")
    settings = Settings()

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        plugins, "create_async_engine", lambda **kwargs: captured.update(kwargs) or MagicMock()
    )
    plugins.create_sqlalchemy_config(settings)
    assert captured["url"] == settings.database.url
    assert captured["connect_args"] == {"ssl": "require"}

    connect = plugins.create_channels_backend(settings)._connect
    assert isinstance(connect, partial)
    assert connect.keywords["dsn"] == settings.database.asyncpg_dsn
    assert connect.keywords["ssl"] == "require"
