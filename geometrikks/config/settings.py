import json
import os
import socket
import ssl
import warnings
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as distribution_version
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import NoDecode, SettingsConfigDict

from geometrikks.config.sources import EnvOrFileSettings
from geometrikks.services.logparser.constants import ALLOWED_GEOIP_LOCALES


def _env_file() -> str | None:
    """Resolve the dotenv path used by every settings section.

    GEOMETRIKKS_ENV_FILE overrides the default ``.env``; an empty value
    disables dotenv loading entirely. The test suite sets it empty before
    this module is imported so results never depend on a developer's local
    ``.env``. Evaluated at class-creation time, like the rest of
    ``model_config``.
    """
    return os.environ.get("GEOMETRIKKS_ENV_FILE", ".env") or None


def get_installed_version() -> str:
    """Return the version of the installed GeoMetrikks distribution.

    The application is packaged during normal ``uv sync`` and image builds, so
    distribution metadata is the single source of truth for the running
    version. The fallback keeps direct source execution usable before install.
    """
    try:
        return distribution_version("geometrikks")
    except PackageNotFoundError:
        return "unknown"


SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
SSLMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]

# libpq connection-string parameters that map onto a DatabaseSettings field.
# Anything else in the query becomes a PostgreSQL startup parameter, which is
# what asyncpg does with the leftovers of a DSN it parses itself.
_DSN_SSL_PARAMS = {
    "sslmode": "sslmode",
    "ssl": "sslmode",
    "sslrootcert": "sslrootcert",
    "sslcert": "sslcert",
    "sslkey": "sslkey",
    "sslpassword": "sslpassword",
}
_DSN_SCHEMES = ("postgresql", "postgres", "postgresql+asyncpg")


def _url_host(host: str) -> str:
    """Bracket a bare IPv6 address so the URL stays parseable."""
    try:
        return f"[{host}]" if ip_address(host).version == 6 else host
    except ValueError:
        return host


def _parse_connection_string(dsn: str, server_settings: Any) -> dict[str, Any]:
    """Turn a libpq connection string into DatabaseSettings field values.

    Keeps close to what asyncpg does with a DSN of its own: the ssl
    parameters configure TLS, and every remaining query parameter becomes a
    PostgreSQL startup parameter. Explicit DB_SERVER_SETTINGS entries win
    over the ones carried by the string.
    """
    parsed = urlsplit(dsn.strip())
    # Never echo the string itself: it carries the password.
    location = parsed.hostname or "the configured host"
    if parsed.scheme.lower() not in _DSN_SCHEMES:
        raise ValueError(
            f"DB_CONNECTION_STRING must start with postgresql:// (got {parsed.scheme or 'no'} "
            "scheme). GeoMetrikks stores its traffic history in PostgreSQL/TimescaleDB; "
            "no other engine is supported."
        )

    fields: dict[str, Any] = {}
    extra: dict[str, str] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        key = key.lower()
        if key in _DSN_SSL_PARAMS:
            fields[_DSN_SSL_PARAMS[key]] = value
        elif key in ("dbname", "database"):
            fields["database"] = value
        elif key in ("host", "port", "user", "password"):
            fields[key] = value
        else:
            extra[key] = value

    if parsed.hostname:
        fields["host"] = parsed.hostname
    try:
        if parsed.port:
            fields["port"] = parsed.port
    except ValueError as exc:  # a port that is not a number
        raise ValueError(f"DB_CONNECTION_STRING for {location} has an invalid port") from exc
    if parsed.username:
        fields["user"] = unquote(parsed.username)
    if parsed.password is not None:
        fields["password"] = unquote(parsed.password)
    name = parsed.path.lstrip("/")
    if name:
        fields["database"] = unquote(name)

    if str(fields.get("host", "")).startswith("/"):
        raise ValueError(
            "DB_CONNECTION_STRING points at a Unix socket; GeoMetrikks connects over "
            "TCP only. Use a host name or address."
        )
    if fields.get("sslmode") and fields["sslmode"] not in SSL_MODES:
        raise ValueError(
            f"DB_CONNECTION_STRING for {location} has sslmode={fields['sslmode']!r}; "
            f"expected one of: {', '.join(SSL_MODES)}"
        )

    if extra:
        explicit = json.loads(server_settings) if isinstance(server_settings, str) else (server_settings or {})
        fields["server_settings"] = {**extra, **explicit}
    return fields


class DatabaseSettings(EnvOrFileSettings):
    """Database configuration settings.

    PostgreSQL with PostGIS is required for this application due to
    GeoAlchemy2 spatial features and high-volume log ingestion.
    """

    # populate_by_name: DB_CONNECTION_STRING/DATABASE_URL are alias-only env
    # names, and _expand_connection_string writes the components it parses
    # back under their field names.
    model_config = SettingsConfigDict(
        env_prefix="DB_", env_file=_env_file(), extra="ignore", populate_by_name=True
    )

    echo: bool = Field(default=False, description="Enable SQLAlchemy query logging")
    echo_pool: bool = Field(default=False, description="Enable SQLAlchemy pool logging")
    max_overflow: int = Field(default=10, description="Max connections above pool_size")
    pool_size: int = Field(default=5, description="Database connection pool size")
    pool_timeout: int = Field(default=30, description="Connection pool timeout in seconds")
    pool_recycle: int = Field(default=3600, description="Connection recycle time in seconds")
    pool_disabled: bool = Field(default=False, description="Disable connection pooling")
    pool_pre_ping: bool = Field(default=True, description="Enable pool pre-ping to check connections")
    user: str = Field(default="geouser", description="Database user")
    password: SecretStr = Field(default=SecretStr("geopass"), description="Database password")
    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, description="Database port")
    database: str = Field(default="geometrikks", description="Database name")
    connection_string: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DB_CONNECTION_STRING", "DATABASE_URL"),
        description=(
            "Complete PostgreSQL connection string, e.g. "
            "postgresql://user:pass@db.example.com:5432/geometrikks?sslmode=require "
            "(DATABASE_URL is accepted as well). Every component it carries wins "
            "over the matching DB_* variable; libpq ssl parameters in the query "
            "populate the DB_SSL* settings and anything else becomes a "
            "PostgreSQL startup parameter."
        ),
    )
    sslmode: SSLMode | None = Field(
        default=None,
        description=(
            "libpq TLS mode: disable, allow, prefer, require, verify-ca or "
            "verify-full. Unset means prefer (TLS when the server offers it, "
            "without verification). verify-ca and verify-full check the server "
            "certificate against DB_SSLROOTCERT, or the system trust store when "
            "no CA file is configured."
        ),
    )
    sslrootcert: Path | None = Field(
        default=None,
        description="Path to the CA certificate that signs the server certificate",
    )
    sslcert: Path | None = Field(
        default=None,
        description="Path to the client certificate for certificate authentication",
    )
    sslkey: Path | None = Field(
        default=None,
        description="Path to the private key belonging to DB_SSLCERT",
    )
    sslpassword: SecretStr | None = Field(
        default=None,
        description="Passphrase protecting DB_SSLKEY, if it is encrypted",
    )
    server_settings: dict[str, str] = Field(
        default_factory=dict,
        description=(
            'PostgreSQL startup parameters as a JSON object, e.g. '
            '{"application_name": "geometrikks"}. Unrecognised query parameters '
            "of DB_CONNECTION_STRING are merged in; explicit entries win."
        ),
    )
    drop_on_startup: bool = Field(default=False, description="Drop all tables on startup (development only)")
    migrate_on_startup: bool = Field(
        default=True,
        description=(
            "Run alembic migrations automatically at app startup. Disable when "
            "migrations run as a separate deployment step (`litestar database "
            "upgrade`); the app then expects the schema to already be at head "
            "and fails startup if it is not usable"
        ),
    )
    startup_wait_seconds: int = Field(
        default=30,
        ge=0,
        description=(
            "How long startup waits for the database before serving in degraded "
            "mode. 0 probes once. The app keeps re-probing in the background after "
            "this window and recovers on its own when the database answers."
        ),
    )
    
    @model_validator(mode="before")
    @classmethod
    def _expand_connection_string(cls, data: Any) -> Any:
        """Split DB_CONNECTION_STRING into the fields the rest of the app reads.

        Expanding before validation (instead of after) means the components
        go through the normal field validation, and the settings API, the
        Status page and every log line show the address actually in use
        rather than an opaque URL.

        The string wins over the individual variables for everything it
        carries: it is one atomic address, and a deployment that sets it
        should not have to unset the DB_HOST an image or compose file
        already provides.
        """
        if not isinstance(data, dict):
            return data
        raw: Any = None
        for key in ("connection_string", "DB_CONNECTION_STRING", "DATABASE_URL"):
            if data.get(key) is not None:
                raw = data.pop(key)
            else:
                data.pop(key, None)
        if raw is None:
            return data
        dsn = raw.get_secret_value() if isinstance(raw, SecretStr) else str(raw)
        data["connection_string"] = dsn
        data.update(_parse_connection_string(dsn, data.get("server_settings")))
        return data

    @model_validator(mode="after")
    def _validate_tls_material(self) -> "DatabaseSettings":
        """Fail on TLS settings that cannot produce a working connection."""
        if self.sslkey is not None and self.sslcert is None:
            raise ValueError("DB_SSLKEY needs DB_SSLCERT: a client key without its certificate is unusable")
        for label, path in (("DB_SSLROOTCERT", self.sslrootcert), ("DB_SSLCERT", self.sslcert), ("DB_SSLKEY", self.sslkey)):
            if path is not None and not path.is_file():
                raise ValueError(f"{label}: certificate file not found: {path}")
        return self

    @property
    def url(self) -> str:
        """Construct the database URL from components.

        Credentials are percent-encoded: reserved URL characters in the
        user or password (@, :, /, %) would otherwise break the URL. TLS
        and startup parameters stay out of the URL and travel through
        ``connect_args`` instead, because SQLAlchemy hands query parameters
        to asyncpg as keyword arguments, which knows ``ssl`` but none of
        libpq's ``ssl*`` spellings.
        """
        return (
            f"postgresql+asyncpg://{quote(self.user, safe='')}:"
            f"{quote(self.password.get_secret_value(), safe='')}"
            f"@{_url_host(self.host)}:{self.port}/{quote(self.database, safe='')}"
        )

    @property
    def asyncpg_dsn(self) -> str:
        """The connection URL as a plain postgresql:// DSN.

        AsyncPgChannelsBackend hands the DSN straight to asyncpg, which does
        not understand SQLAlchemy's +asyncpg driver suffix.
        """
        return self.url.replace("postgresql+asyncpg://", "postgresql://", 1)

    @property
    def connect_args(self) -> dict[str, Any]:
        """asyncpg connect kwargs shared by the engine and the channels backend.

        Both connect to the same server, so TLS and startup parameters are
        resolved once here instead of once per URL spelling.
        """
        args: dict[str, Any] = {}
        ssl_argument = self.ssl_argument
        if ssl_argument is not None:
            args["ssl"] = ssl_argument
        if self.server_settings:
            args["server_settings"] = dict(self.server_settings)
        return args

    @property
    def ssl_argument(self) -> ssl.SSLContext | str | bool | None:
        """asyncpg's ``ssl`` argument for the configured mode.

        Bare modes are passed through as strings so asyncpg applies libpq's
        own semantics. A context is only built when certificate files are
        involved, or for verify-ca/verify-full without a CA file: asyncpg
        would then insist on ~/.postgresql/root.crt, while a managed
        provider's publicly signed certificate verifies against the system
        trust store.
        """
        certs_configured = any((self.sslrootcert, self.sslcert, self.sslkey))
        if self.sslmode is None and not certs_configured:
            return None  # asyncpg's default: prefer, i.e. TLS when offered
        if self.sslmode == "disable":
            return False
        mode = self.sslmode or "prefer"
        if not certs_configured and mode in ("allow", "prefer", "require"):
            return mode
        return self._build_ssl_context(mode)

    def _build_ssl_context(self, mode: str) -> ssl.SSLContext:
        """Mirror libpq's verification rules for the given mode."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = mode == "verify-full"
        if mode in ("allow", "prefer"):
            context.verify_mode = ssl.CERT_NONE
        elif self.sslrootcert is not None:
            context.load_verify_locations(cafile=str(self.sslrootcert))
            context.verify_mode = ssl.CERT_REQUIRED
        elif mode == "require":
            # require encrypts but does not authenticate; only the verify-*
            # modes promise the server is who it claims to be.
            context.verify_mode = ssl.CERT_NONE
        else:
            context.load_default_certs(ssl.Purpose.SERVER_AUTH)
            context.verify_mode = ssl.CERT_REQUIRED
        if self.sslcert is not None:
            password = self.sslpassword.get_secret_value() if self.sslpassword else None
            context.load_cert_chain(
                str(self.sslcert),
                keyfile=str(self.sslkey) if self.sslkey else None,
                password=password,
            )
        return context

    @model_validator(mode="after")
    def validate_db_url(self) -> "DatabaseSettings":
        """Ensure database URL is a valid PostgreSQL connection string."""
        if not self.url.startswith(("postgresql", "postgresql+asyncpg")):
            raise ValueError(
                "Database URL must be PostgreSQL with asyncpg driver. "
                "Example: postgresql+asyncpg://user:pass@localhost/geometrikks"
            )
        return self


class GeoIPSettings(EnvOrFileSettings):
    """GeoIP database configuration settings."""

    # populate_by_name: account_id/license_key use MAXMINDDB_* validation
    # aliases for the env vars but must stay constructible by field name.
    model_config = SettingsConfigDict(
        env_prefix="GEOIP_", env_file=_env_file(), extra="ignore", populate_by_name=True
    )

    db_path: Path = Field(
        default=Path("data/geoip/GeoLite2-City.mmdb"),
        description="Path to GeoIP2/GeoLite2 database file",
    )
    locales: list[str] = Field(
        default=["en"],
        description="List of GeoIP locales to use",
    )
    validate_db_path: bool = Field(
        default=False,
        description=(
            "Fail settings validation when the GeoIP database file is missing. "
            "Off by default: the auto-downloader/degraded-mode path owns the "
            "missing-file case (set true to fail fast instead)."
        ),
    )
    validate_locales: bool = Field(
        default=True,
        description="Validate that the specified GeoIP locales are supported"
    )
    account_id: str | None = Field(
        default=None,
        validation_alias="MAXMINDDB_USER_ID",
        description="MaxMind account ID for GeoLite2 auto-download",
    )
    license_key: SecretStr | None = Field(
        default=None,
        validation_alias="MAXMINDDB_LICENSE_KEY",
        description="MaxMind license key for GeoLite2 auto-download",
    )
    refresh_days: int = Field(
        default=7, description="Re-download the GeoLite2 database when older than this many days"
    )
    asn_db_path: Path = Field(
        default=Path("data/geoip/GeoLite2-ASN.mmdb"),
        description="Path to the GeoLite2 ASN database file",
    )
    asn_enabled: bool = Field(
        default=True,
        description=(
            "Download and use the GeoLite2 ASN database for per-request "
            "ASN/organization enrichment. Uses the same MaxMind credentials as "
            "the City database; without credentials or a database file the app "
            "simply ingests without ASN data."
        ),
    )

    @model_validator(mode="after")
    def validate_geoip_db_exists(self) -> "GeoIPSettings":
        """Ensure GeoIP database file exists if validation is enabled.

        Resolves relative paths from the project root to work in all contexts.
        """
        project_root = Path(__file__).parent.parent.parent
        db_path = self.db_path

        # If path is relative, resolve from project root
        if not db_path.is_absolute():
            db_path = project_root / db_path

        if self.validate_db_path and not db_path.exists():
            raise ValueError(f"GeoIP database file not found: {db_path}")

        # Update the path to absolute for runtime use
        self.db_path = db_path

        # Same resolution for the ASN path, but no existence check; the
        # downloader handles a missing file.
        asn_db_path = self.asn_db_path
        if not asn_db_path.is_absolute():
            asn_db_path = project_root / asn_db_path
        self.asn_db_path = asn_db_path
        return self

    @model_validator(mode="after")
    def validate_geoip_locales(self) -> "GeoIPSettings":
        """Ensure GeoIP locales are valid if validation is enabled."""
        if self.validate_locales:
            invalid_locales = [loc for loc in self.locales if loc not in ALLOWED_GEOIP_LOCALES]
            if invalid_locales:
                raise ValueError(f"Invalid GeoIP locales: {invalid_locales}. Allowed locales are: {ALLOWED_GEOIP_LOCALES}")
        return self


class APISettings(EnvOrFileSettings):
    """API server configuration settings."""

    model_config = SettingsConfigDict(env_prefix="API_", env_file=_env_file(), extra="ignore")

    host: str = Field(default="0.0.0.0", description="API server host")
    port: int = Field(default=8000, description="API server port")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = Field(
        default=None,
        description="DEPRECATED: use LOG_LEVEL. Kept as a fallback for existing deployments.",
    )


class LogSettings(EnvOrFileSettings):
    """Application logging configuration (files, rotation, level)."""

    model_config = SettingsConfigDict(env_prefix="LOG_", env_file=_env_file(), extra="ignore")

    dir: Path = Field(default=Path("logs"), description="Directory for application log files")
    level: Literal["DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"] | None = Field(
        default=None,
        description="Root log level. Falls back to deprecated API_LOG_LEVEL, then INFO.",
    )
    main_max_bytes: int = Field(
        default=10 * 1024 * 1024,
        description="Rotate the main JSONL log when it exceeds this size (bytes)",
    )
    main_backup_count: int = Field(
        default=5, description="Number of gzipped main-log archives to keep"
    )
    login_max_bytes: int = Field(
        default=10 * 1024 * 1024,
        description="Rotate the login log when it exceeds this size (bytes)",
    )
    login_backup_count: int = Field(
        default=5, description="Number of gzipped login-log archives to keep"
    )


class LogParserSettings(EnvOrFileSettings):
    """Log parser configuration settings."""

    model_config = SettingsConfigDict(env_prefix="LOGPARSER_", env_file=_env_file(), extra="ignore")

    enabled: bool = Field(
        default=True,
        description="Enable log parser ingestion service"
    )
    log_paths: Annotated[list[Path], NoDecode] = Field(
        default_factory=lambda: [Path("/var/log/access/access.log")],
        min_length=1,
        description=(
            "Access log files to tail. Env accepts a single path or a JSON "
            "list of paths. Default: /var/log/access/access.log"
        ),
    )
    log_formats: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["auto"],
        description=(
            "Log format per tailed file: 'auto' (default, detected from the "
            "file's content), 'geometrikks-json', 'nginx', 'traefik-json', "
            "or 'caddy-json'. Env accepts a single value applied to every "
            "path, or a JSON list matching LOGPARSER_LOG_PATHS by position."
        ),
    )
    poll_interval: float = Field(
        default=1.0,
        description="Interval in seconds to poll the log file for new entries",
    )
    send_logs: bool = Field(default=True, description="Send parsed logs to the database")
    host_name: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [socket.gethostname()],
        min_length=1,
        description=(
            "Source hostname stamped on ingested records. Env accepts a "
            "single value applied to every tailed file, or a JSON list "
            "matching LOGPARSER_LOG_PATHS by position. Default: this "
            "machine's hostname."
        ),
    )
    batch_size: int = Field(
        default=100,
        description="Max records before forced commit.",
    )
    commit_interval: float = Field(
        default=5.0,
        description="Maximum time interval in seconds between database commits. This will commit even if batch_size is not reached.",
    )
    skip_validation : bool = Field(
        default=False,
        description="Skip validation of log lines.",
    )
    store_debug_lines: bool = Field(
        default=False,
        description="Store all raw log lines in AccessLogDebug table. When False, only malformed requests are stored.",
    )
    ignore_ips: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "IPs/CIDRs the parser drops entirely (no geo event, access log, "
            "or debug row). Use for your own traffic hitting the reverse "
            "proxy. Env accepts one value, comma-separated values, or a "
            "JSON list. Empty (default): nothing is ignored."
        ),
    )

    @field_validator("log_paths", mode="before")
    @classmethod
    def parse_log_paths(cls, value: object) -> object:
        """Accept a single path (str/Path) or a JSON list of paths."""
        if isinstance(value, Path):
            return [value]
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [stripped]
        return value

    @field_validator("log_formats", mode="before")
    @classmethod
    def parse_log_formats(cls, value: object) -> object:
        """Accept a single format name or a JSON list of names."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [stripped]
        return value

    @model_validator(mode="after")
    def validate_log_formats(self) -> "LogParserSettings":
        """Reject unknown format names and lengths that cannot map to log_paths."""
        from geometrikks.services.logparser.formats import FORMATS

        allowed = {"auto", *FORMATS}
        unknown = [f for f in self.log_formats if f not in allowed]
        if unknown:
            raise ValueError(f"Unknown log format(s) {unknown}; allowed: {sorted(allowed)}")
        if len(self.log_formats) not in (1, len(self.log_paths)):
            raise ValueError(
                "LOGPARSER_LOG_FORMATS must be one value or match "
                f"LOGPARSER_LOG_PATHS in length ({len(self.log_paths)})"
            )
        return self

    def resolved_formats(self) -> list[str]:
        """Return one format per log path.

        Returns:
            The configured formats, fanning a single value out across all
            log paths when only one value was provided.
        """
        if len(self.log_formats) == 1:
            return self.log_formats * len(self.log_paths)
        return list(self.log_formats)

    @field_validator("host_name", mode="before")
    @classmethod
    def parse_host_name(cls, value: object) -> object:
        """Accept a single hostname or a JSON list of hostnames."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [stripped]
        return value

    @field_validator("host_name")
    @classmethod
    def validate_host_name_entries(cls, value: list[str]) -> list[str]:
        """Fail at startup on empty entries; '' would silently un-stamp records."""
        if any(not entry.strip() for entry in value):
            raise ValueError("LOGPARSER_HOST_NAME entries must be non-empty")
        return [entry.strip() for entry in value]

    @model_validator(mode="after")
    def validate_host_name_length(self) -> "LogParserSettings":
        """Reject hostname list lengths that cannot map to log_paths."""
        if len(self.host_name) not in (1, len(self.log_paths)):
            raise ValueError(
                "LOGPARSER_HOST_NAME must be one value or match "
                f"LOGPARSER_LOG_PATHS in length ({len(self.log_paths)})"
            )
        return self

    def resolved_hostnames(self) -> list[str]:
        """Return one hostname per log path.

        Returns:
            The configured hostnames, fanning a single value out across all
            log paths when only one value was provided.
        """
        if len(self.host_name) == 1:
            return self.host_name * len(self.log_paths)
        return list(self.host_name)

    @field_validator("ignore_ips", mode="before")
    @classmethod
    def parse_ignore_ips(cls, value: object) -> object:
        """Accept one value, comma-separated values, or a JSON list."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

    @field_validator("ignore_ips")
    @classmethod
    def validate_ignore_ips(cls, value: list[str]) -> list[str]:
        """Fail at startup on entries that are not an IP or CIDR."""
        for entry in value:
            try:
                ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"LOGPARSER_IGNORE_IPS entry {entry!r} is not an IP address or CIDR"
                ) from exc
        return value


class AnalyticsSettings(EnvOrFileSettings):
    """Analytics and aggregation configuration settings.

    TimescaleDB handles retention via policies configured in lifecycle.py.
    These settings define the default retention periods.
    """

    model_config = SettingsConfigDict(env_prefix="ANALYTICS_", env_file=_env_file(), extra="ignore")

    # Retention periods for TimescaleDB policies
    raw_retention_days: int = Field(
        default=180,
        description=(
            "Days to keep raw geo_events and access_logs data. At least 4: the "
            "daily aggregates refresh their last 3 days from raw rows, and a "
            "shorter retention makes each refresh erase those buckets. Startup "
            "refuses lower values."
        ),
    )
    debug_retention_days: int = Field(
        default=30,
        description="Days to keep access_log_debug data",
    )
    hourly_retention_days: int = Field(
        default=60,
        description="Days to keep hourly continuous aggregate data",
    )
    # Daily aggregates are permanent (no retention)

    # Continuous aggregate refresh settings
    cagg_refresh_interval_minutes: int = Field(
        default=5,
        description="Minutes between continuous aggregate refreshes",
    )

    # Compression settings
    compression_after_days: int = Field(
        default=7,
        description="Days after which to compress hypertable chunks",
    )



class SchedulerSettings(EnvOrFileSettings):
    """APScheduler configuration for periodic background tasks."""

    model_config = SettingsConfigDict(env_prefix="SCHEDULER_", env_file=_env_file(), extra="ignore")

    enabled: bool = Field(
        default=True,
        description="Enable scheduled background tasks",
    )
    location_refresh_interval_minutes: int = Field(
        default=10,
        description="Minutes between GeoLocation.last_hit refresh jobs",
    )


class MapSettings(EnvOrFileSettings):
    """Map presentation settings shared with the web client."""

    model_config = SettingsConfigDict(env_prefix="MAP_", env_file=_env_file(), extra="ignore")

    home_latitude: float | None = Field(
        default=None,
        ge=-90,
        le=90,
        description=(
            "Optional destination latitude for live request routes. Set both home "
            "coordinates to override external-IP auto-detection."
        ),
    )
    home_longitude: float | None = Field(
        default=None,
        ge=-180,
        le=180,
        description=(
            "Optional destination longitude for live request routes. Set both home "
            "coordinates to override external-IP auto-detection."
        ),
    )
    auto_detect_home: bool = Field(
        default=True,
        description=(
            "Resolve the server's public IP at startup and geolocate it when "
            "home coordinates are unset."
        ),
    )
    public_ip_url: str = Field(
        default="https://api64.ipify.org?format=json",
        description=(
            "JSON endpoint used for public-IP discovery; the response must "
            "contain an 'ip' field."
        ),
    )
    public_ip_timeout: float = Field(
        default=3.0,
        gt=0,
        le=30,
        description="Timeout in seconds for public-IP discovery.",
    )
    home_locations: dict[str, tuple[float, float]] = Field(
        default_factory=dict,
        description=(
            "Per-hostname home overrides as a JSON object of "
            '{"hostname": [latitude, longitude]}. Overrides win over '
            "agent auto-detection in site_homes; removing an entry deletes "
            "its override row at the next startup. Use for sites whose "
            "public IP geolocates wrong (CGNAT, VPN) or for hostnames in "
            "logs shipped from other machines."
        ),
    )

    home_refresh_hours: int = Field(
        default=24,
        ge=1,
        le=24 * 30,
        description=(
            "How often this instance re-detects its own public-IP home "
            "location and refreshes its site_homes rows (hours)."
        ),
    )
    carto_api_key: str = Field(
        default="",
        description=(
            "CARTO basemaps API key, sent as ?key= on every basemap request "
            "the browser makes. CARTO's terms require one per deployment; "
            "keys are free at https://carto.com/basemaps/apikey. Not a "
            "secret: it is visible to anyone who can open the map."
        ),
    )

    @field_validator("home_locations")
    @classmethod
    def validate_home_locations(cls, value: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
        """Coordinate ranges; pydantic already enforced the two-float arity."""
        for hostname, (lat, lng) in value.items():
            if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
                raise ValueError(
                    f"MAP_HOME_LOCATIONS[{hostname!r}]: latitude must be in "
                    "[-90, 90] and longitude in [-180, 180]"
                )
        return value

    @model_validator(mode="after")
    def validate_home_coordinate_pair(self) -> "MapSettings":
        """Require both manual coordinates or neither."""
        if (self.home_latitude is None) != (self.home_longitude is None):
            raise ValueError("MAP_HOME_LATITUDE and MAP_HOME_LONGITUDE must be set together")
        return self


class CrowdSecSettings(EnvOrFileSettings):
    """CrowdSec Local API integration settings.

    The integration is enabled when ``lapi_url`` and ``bouncer_api_key`` are
    set. The bouncer API key enables read-only decision access; machine
    credentials additionally enable ban/unban actions.
    """

    model_config = SettingsConfigDict(env_prefix="CROWDSEC_", env_file=_env_file(), extra="ignore")

    lapi_url: str | None = Field(
        default=None,
        description="CrowdSec Local API base URL, e.g. http://crowdsec:8080",
    )
    bouncer_api_key: SecretStr | None = Field(
        default=None,
        description="Bouncer API key (cscli bouncers add geometrikks) - read access",
    )
    machine_id: str | None = Field(
        default=None,
        description="Machine ID (cscli machines add) - enables ban/unban",
    )
    machine_password: SecretStr | None = Field(
        default=None,
        description="Machine password - enables ban/unban",
    )
    default_ban_duration: str = Field(
        default="4h",
        description="Default duration for manual bans (Go duration string)",
    )
    request_timeout: float = Field(default=10.0, description="LAPI request timeout in seconds")
    verify_tls: bool = Field(default=True, description="Verify TLS when LAPI uses https")
    stream_poll_interval: float = Field(
        default=15.0,
        gt=0,
        description="Seconds between decision-stream polls feeding live ban/unban updates",
    )

    @property
    def enabled(self) -> bool:
        """Read access is available: LAPI URL and bouncer key are both set."""
        return self.lapi_url is not None and self.bouncer_api_key is not None

    @property
    def write_enabled(self) -> bool:
        """Ban/unban is available: read access plus machine credentials."""
        return self.enabled and self.machine_id is not None and self.machine_password is not None

    @model_validator(mode="after")
    def validate_machine_credential_pair(self) -> "CrowdSecSettings":
        """Require both machine credentials or neither.

        Half-configured write credentials should fail at startup, not at the
        first ban attempt.
        """
        if (self.machine_id is None) != (self.machine_password is None):
            raise ValueError(
                "CROWDSEC_MACHINE_ID and CROWDSEC_MACHINE_PASSWORD must be set together"
            )
        return self


class ViteSettings(EnvOrFileSettings):
    """Vite server configuration settings."""

    model_config = SettingsConfigDict(env_prefix="VITE_", env_file=_env_file(), extra="ignore")

    dev_mode: bool = Field(
        default=False,
        description="Start vite development server."
    )
    use_server_lifespan: bool = Field(
        default=True,
        description="Auto start and stop vite processes when running in development mode."
    )
    host: str = Field(
        default="0.0.0.0",
        description="The host the vite process will listen on. Defaults to 0.0.0.0."
    )
    port: int = Field(
        default=5173,
        description="The port to start vite on. Default is 5173."
    )
    enable_react_helpers: bool = Field(
        default=True,
        description="Enable React support in HMR."
    )
    http2: bool = Field(
        default=True,
        description="Enable HTTP/2 for the Vite development server."
    )

    executor: Literal["node", "bun", "deno", "yarn", "pnpm"] | None = Field(
        default="bun",
        description="JS runtime executor for litestar-vite (defaults to bun).",
    )


class AppSettings(EnvOrFileSettings):
    """Application-level settings."""

    model_config = SettingsConfigDict(env_prefix="APP_", env_file=_env_file(), extra="ignore")

    mode: Literal["full", "agent"] = Field(
        default="full",
        description="Application mode: full (all components) or agent (logparser only)"
    )
    proxy_advisory: bool = Field(
        default=True,
        description=(
            "Warn on Settings > Status when most recent traffic for a tailed "
            "file comes from CDN or private peer addresses, meaning the proxy "
            "logs its upstream instead of the visitor. CDN findings for "
            "agent-tailed sources are scanned from the database on the head "
            "every 5 minutes. Set to false when the traffic mix is deliberate "
            "(Tailscale-only access, a CDN you front on purpose)."
        ),
    )


class Settings(EnvOrFileSettings):
    """Main application settings.
    
    This class aggregates all configuration sections and provides
    a single point of access for application configuration.
    
    Configuration precedence (highest to lowest):
    1. Environment variables
    2. .env file
    3. Default values
    
    Example .env file:
        APP_NAME=GeoMetrikks
        APP_DEBUG=true
        GEOIP_DB_PATH=data/GeoLite2-City.mmdb
    """

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application metadata
    name: str = Field(default="GeoMetrikks API", description="Application name")
    version: str = Field(
        default_factory=get_installed_version,
        description="Application version (defaults to installed package metadata)",
    )
    description: str = Field(
        default="Real-time GeoIP lookups and traffic analytics API",
        description="Application description",
    )
    debug: bool = Field(default=False, description="Enable debug mode")
    environment: Literal["development", "staging", "production"] = Field(
        default="production",
        description="Application environment",
    )
    runtime: Literal["host", "container"] = Field(
        default="host",
        description="Execution runtime; container images set this to container.",
    )
    image_tag: str | None = Field(
        default=None,
        description="Optional container image tag embedded at build time.",
    )

    # Authentication (single admin user; see Phase 1c design)
    auth_disabled: bool = Field(
        default=False,
        description=(
            "Disable the built-in session auth entirely. Set true only when an "
            "authenticating reverse proxy (Authelia, Tailscale, ...) fronts the app."
        ),
    )
    admin_user: str = Field(default="admin", description="Admin login username")
    admin_password: SecretStr | None = Field(
        default=None,
        description="Admin login password (required unless auth_disabled=true)",
    )
    session_secure: bool = Field(
        default=False,
        description=(
            "Mark the session cookie Secure so browsers only send it over "
            "HTTPS. Recommended when serving behind a TLS reverse proxy."
        ),
    )
    trusted_proxies: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Reverse-proxy IPs/CIDRs allowed to supply X-Forwarded-For. Env "
            "accepts one value, comma-separated values, or a JSON list. "
            "Empty (default): forwarded headers are never trusted."
        ),
    )

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def parse_trusted_proxies(cls, value: object) -> object:
        """Accept one value, comma-separated values, or a JSON list."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

    @field_validator("trusted_proxies")
    @classmethod
    def validate_trusted_proxies(cls, value: list[str]) -> list[str]:
        """Fail at startup on entries that are not an IP or CIDR."""
        for entry in value:
            try:
                ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"APP_TRUSTED_PROXIES entry {entry!r} is not an IP address or CIDR"
                ) from exc
        return value

    @model_validator(mode="after")
    def _resolve_log_level(self) -> "Settings":
        """LOG_LEVEL wins; deprecated API_LOG_LEVEL is honored with a warning."""
        if self.log.level is None:
            if self.api.log_level is not None:
                warnings.warn(
                    "API_LOG_LEVEL is deprecated and will be removed in a future "
                    "release; set LOG_LEVEL instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                self.log.level = self.api.log_level
            else:
                self.log.level = "INFO"
        return self

    # Sub-configurations
    app: AppSettings = Field(default_factory=AppSettings)
    api: APISettings = Field(default_factory=APISettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    geoip: GeoIPSettings = Field(default_factory=GeoIPSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    logparser: LogParserSettings = Field(default_factory=LogParserSettings)
    analytics: AnalyticsSettings = Field(default_factory=AnalyticsSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    map: MapSettings = Field(default_factory=MapSettings)
    crowdsec: CrowdSecSettings = Field(default_factory=CrowdSecSettings)
    vite: ViteSettings = Field(default_factory=ViteSettings)

    @model_validator(mode="after")
    def validate_agent_tails_something(self) -> "Settings":
        """APP_MODE=agent with LOGPARSER_ENABLED=false is a no-op process."""
        if self.app.mode == "agent" and not self.logparser.enabled:
            raise ValueError(
                "APP_MODE=agent requires LOGPARSER_ENABLED=true: an agent "
                "that tails nothing does nothing"
            )
        return self

    @property
    def is_agent(self) -> bool:
        """Check if running in agent mode."""
        return self.app.mode == "agent"

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.
    
    This function is cached to ensure we only parse configuration once.
    Use this function throughout the application to access settings.
    
    Returns:
        Settings: Application settings instance
    """
    return Settings()
