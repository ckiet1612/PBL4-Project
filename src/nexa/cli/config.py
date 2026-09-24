"""Local product CLI configuration and context resolution."""

from __future__ import annotations

import os
import stat
import tempfile
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .errors import CliError


@dataclass(frozen=True)
class Profile:
    endpoint: str = "http://127.0.0.1:8000"
    tenant_id: str | None = None


@dataclass(frozen=True)
class CliConfig:
    profiles: dict[str, Profile] = field(default_factory=dict)
    active_profile: str = "default"


@dataclass(frozen=True)
class ResolvedContext:
    endpoint: str
    profile: str
    tenant_id: str | None


class ConfigStore:
    def __init__(self, root: Path | str | None = None) -> None:
        if root is None:
            root = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
            root = Path(root) / "nexa"
        self.root = Path(root)
        self.config_path = self.root / "config.toml"
        self.credentials_path = self.root / "credentials.json"

    def load(self) -> CliConfig:
        if self._exists_or_link(self.root):
            self._require_private(self.root, 0o700, "configuration directory")
        if not self._exists_or_link(self.config_path):
            return CliConfig(profiles={"default": Profile()})
        self._require_private(self.config_path, 0o600, "configuration file")
        try:
            with self.config_path.open("rb") as stream:
                raw = tomllib.load(stream)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise CliError("local configuration is invalid", exit_code=2) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("profiles", {}), dict):
            raise CliError("local configuration is invalid", exit_code=2)
        active_profile = raw.get("active_profile", "default")
        if not isinstance(active_profile, str):
            raise CliError("local configuration is invalid", exit_code=2)
        profiles: dict[str, Profile] = {}
        for name, values in raw.get("profiles", {}).items():
            if not isinstance(name, str) or not isinstance(values, dict):
                raise CliError("local configuration is invalid", exit_code=2)
            endpoint = values.get("endpoint", Profile.endpoint)
            tenant_id = values.get("tenant_id")
            if not isinstance(endpoint, str) or (
                tenant_id is not None and not isinstance(tenant_id, str)
            ):
                raise CliError("local configuration is invalid", exit_code=2)
            profiles[name] = Profile(endpoint=endpoint, tenant_id=tenant_id)
        if not profiles:
            profiles["default"] = Profile()
        return CliConfig(profiles=profiles, active_profile=active_profile)

    def save(self, config: CliConfig) -> None:
        lines = [f'active_profile = "{_toml_quote(config.active_profile)}"', "", "[profiles]"]
        for name in sorted(config.profiles):
            profile = config.profiles[name]
            lines.extend(
                [f"[profiles.{_toml_key(name)}]", f'endpoint = "{_toml_quote(profile.endpoint)}"']
            )
            if profile.tenant_id is not None:
                lines.append(f'tenant_id = "{_toml_quote(profile.tenant_id)}"')
            lines.append("")
        self._atomic_write(self.config_path, "\n".join(lines).rstrip() + "\n")

    def save_token(self, profile: str, token: str) -> None:
        import json

        credentials: dict[str, str] = {}
        if self._exists_or_link(self.credentials_path):
            self._require_private(self.root, 0o700, "configuration directory")
            self._require_private(self.credentials_path, 0o600, "credentials file")
            try:
                loaded = json.loads(self.credentials_path.read_text())
                if isinstance(loaded, dict):
                    credentials = {str(k): str(v) for k, v in loaded.items()}
            except (OSError, ValueError):
                credentials = {}
        credentials[profile] = token
        self._atomic_write(
            self.credentials_path,
            json.dumps(credentials, sort_keys=True, separators=(",", ":")) + "\n",
        )

    def get_token(self, profile: str) -> str | None:
        import json

        if self._exists_or_link(self.root):
            self._require_private(self.root, 0o700, "configuration directory")
        if not self._exists_or_link(self.credentials_path):
            return None
        self._require_private(self.credentials_path, 0o600, "credentials file")
        try:
            raw = json.loads(self.credentials_path.read_text())
        except (OSError, ValueError) as exc:
            raise CliError("local credentials are invalid", exit_code=2) from exc
        value = raw.get(profile) if isinstance(raw, dict) else None
        return value if isinstance(value, str) else None

    @staticmethod
    def _exists_or_link(path: Path) -> bool:
        return path.exists() or path.is_symlink()

    @staticmethod
    def _require_private(path: Path, mode: int, label: str) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise CliError(f"{label} is unavailable", exit_code=2) from exc
        if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) != mode:
            raise CliError(f"{label} permissions are insecure", exit_code=2)

    def _atomic_write(self, path: Path, content: str) -> None:
        temporary_path: Path | None = None
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.root)
            temporary_path = Path(temporary)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
            os.chmod(path, 0o600)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise CliError("local configuration could not be written", exit_code=2) from exc
        finally:
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)


def resolve_context(
    config: CliConfig,
    *,
    endpoint: str | None,
    profile: str | None,
    tenant_id: str | None,
    environ: Mapping[str, str],
) -> ResolvedContext:
    selected = profile or environ.get("NEXA_PROFILE") or config.active_profile
    selected_profile = config.profiles.get(selected)
    if selected_profile is None:
        raise CliError(f"profile does not exist: {selected}", exit_code=2)
    return ResolvedContext(
        endpoint=endpoint or environ.get("NEXA_ENDPOINT") or selected_profile.endpoint,
        profile=selected,
        tenant_id=tenant_id or environ.get("NEXA_TENANT_ID") or selected_profile.tenant_id,
    )


def resolve_token(
    store: ConfigStore,
    profile: str,
    *,
    environ: Mapping[str, str],
    stdin: TextIO | None = None,
) -> str | None:
    if stdin is not None:
        try:
            token = stdin.read().strip()
        except OSError as exc:
            raise CliError("unable to read token from stdin", exit_code=2) from exc
        if not token:
            raise CliError("token from stdin is empty", exit_code=2)
        return token
    token = environ.get("NEXA_TOKEN")
    return token if token else store.get_token(profile)


def _toml_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _toml_key(value: str) -> str:
    return '"' + _toml_quote(value) + '"' if not value.replace("_", "").isalnum() else value
