"""Locates config/ and data/ and loads YAML config and .env secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILES = ("risk", "red-flags", "valuation", "kite", "tax", "sectors")


class ConfigError(Exception):
    pass


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Settings:
    home: Path

    @property
    def config_dir(self) -> Path:
        return self.home / "config"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ltinv.db"

    @property
    def thesis_dir(self) -> Path:
        return self.data_dir / "thesis"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def load(self, name: str) -> dict[str, Any]:
        path = self.config_dir / f"{name}.yaml"
        if not path.is_file():
            raise ConfigError(f"missing config file: {path} (run `ltinv init` to create it)")
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        return data

    def env(self, key: str) -> str | None:
        """Process environment first, then <home>/.env. Empty values count as unset."""
        value = os.environ.get(key)
        if not value:
            value = _parse_env_file(self.home / ".env").get(key)
        return value or None

    def require_env(self, key: str) -> str:
        value = self.env(key)
        if not value:
            raise ConfigError(f"{key} is not set (put it in {self.home / '.env'})")
        return value


def write_missing_config(settings: Settings) -> list[str]:
    """Copy packaged example config for any file missing under <home>/config.
    Your real config holds personal data (sectors = what you hold) and lives outside the
    public code repo; existing files are never overwritten."""
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    examples = resources.files("ltinv") / "config_examples"
    written = []
    for name in CONFIG_FILES:
        target = settings.config_dir / f"{name}.yaml"
        if not target.exists():
            target.write_text((examples / f"{name}.yaml").read_text())
            written.append(target.name)
    return written


def get_settings(home: str | Path | None = None) -> Settings:
    base = home or os.environ.get("LTINV_HOME") or Path.cwd()
    return Settings(home=Path(base).expanduser().resolve())
