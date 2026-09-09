"""Settings sources shared by every configuration section.

Every variable the app understands can also be supplied indirectly as
``<VARIABLE>_FILE``, pointing at a file that holds the value. Container
secret stores hand credentials over as mounted files (Docker/Podman
secrets under ``/run/secrets``, Kubernetes secret volumes), which keeps
them out of ``docker inspect``, ``/proc/<pid>/environ`` and crash reports.

The indirection sits above the plain environment variable and below
explicit constructor arguments, so a deployment that sets both
``DB_PASSWORD`` and ``DB_PASSWORD_FILE`` gets the file: the file is the
deliberate choice, the plain variable is usually an inherited default.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import dotenv_values
from pydantic_settings import BaseSettings, EnvSettingsSource, SettingsError

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo
    from pydantic_settings.sources import PydanticBaseSettingsSource

FILE_SUFFIX = "_FILE"


def _read_secret_file(path: str, variable: str) -> str:
    """Return the file's contents without the trailing newline a writer adds.

    Only line endings are stripped: leading and inner whitespace can be part
    of a password.
    """
    try:
        content = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsError(f"{variable}={path!r} could not be read: {exc}") from exc
    return content.rstrip("\r\n")


class FileSecretEnvSettingsSource(EnvSettingsSource):
    """Resolve settings from the file that ``<VARIABLE>_FILE`` points at.

    Subclasses EnvSettingsSource so prefixes, aliases, case handling and
    complex-value decoding stay identical to the plain environment source;
    only the lookup changes. Files are read lazily, one per field that is
    actually configured, so an unrelated ``*_FILE`` variable is never opened.
    """

    def __init__(self, settings_cls: type[BaseSettings], env_file: Any = None) -> None:
        # Before super().__init__(): it calls _load_env_vars(), which needs it.
        # The dotenv source resolves _env_file overrides passed to a
        # constructor, so honoring the same value keeps the two in step.
        self._env_file = env_file if env_file is not None else settings_cls.model_config.get("env_file")
        super().__init__(settings_cls)

    def _load_env_vars(self) -> Mapping[str, str | None]:
        """Map ``DB_PASSWORD_FILE=/run/secrets/db`` to ``db_password -> /run/secrets/db``.

        Values are paths, not settings values; ``get_field_value`` reads them.
        Real environment variables win over ``.env`` entries, matching the
        precedence the dotenv source itself uses.
        """
        pointers: dict[str, str | None] = {}
        for source in (self._dotenv_values(), os.environ):
            for key, value in source.items():
                if not value or len(key) <= len(FILE_SUFFIX):
                    continue
                if key[-len(FILE_SUFFIX) :].upper() != FILE_SUFFIX:
                    continue
                name = key[: -len(FILE_SUFFIX)]
                pointers[name if self.case_sensitive else name.lower()] = value
        return pointers

    def _dotenv_values(self) -> Mapping[str, str | None]:
        """``*_FILE`` pointers configured in the dotenv file(s), if any."""
        env_file = self._env_file
        if not env_file:
            return {}
        paths = [env_file] if isinstance(env_file, (str, os.PathLike)) else list(env_file)
        values: dict[str, str | None] = {}
        for path in paths:
            candidate = Path(path).expanduser()
            if candidate.is_file():
                values.update(dotenv_values(candidate, encoding=self.config.get("env_file_encoding") or "utf-8"))
        return values

    def __call__(self) -> dict[str, Any]:
        """Name the offending ``*_FILE`` variable, not just the field.

        The base class reports which field failed and drops the reason into
        the exception chain, where a startup log line does not show it.
        """
        try:
            return super().__call__()
        except SettingsError as exc:
            cause = exc.__cause__
            raise SettingsError(f"{exc}: {cause}" if cause else str(exc)) from cause

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        field_key = field_name
        value_is_complex = False
        for field_key, env_name, value_is_complex in self._extract_field_info(field, field_name):
            path = self.env_vars.get(env_name)
            if path is not None:
                variable = f"{env_name if self.case_sensitive else env_name.upper()}{FILE_SUFFIX}"
                return _read_secret_file(path, variable), field_key, value_is_complex
        return None, field_key, value_is_complex


class EnvOrFileSettings(BaseSettings):
    """Base class that adds ``<VARIABLE>_FILE`` support to a settings section."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            FileSecretEnvSettingsSource(settings_cls, getattr(dotenv_settings, "env_file", None)),
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )
