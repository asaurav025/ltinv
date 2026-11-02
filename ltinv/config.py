"""Locates config/ and data/ and loads YAML config and .env secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
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
            raise ConfigError(f"missing config file: {path}")
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


def get_settings(home: str | Path | None = None) -> Settings:
    base = home or os.environ.get("LTINV_HOME") or Path.cwd()
    return Settings(home=Path(base).expanduser().resolve())
