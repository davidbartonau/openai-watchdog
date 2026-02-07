"""YAML configuration for openai-watchdog.

Loads and validates a ``watchdog.yaml`` file that defines key groups,
rate-limit thresholds, alert settings, and export preferences.

Example config::

    admin_key_env: OPENAI_ADMIN_KEY

    key_groups:
      team-alpha:
        api_key_ids:
          - sk-proj-abc123
          - sk-proj-def456
        max_dollars_per_hour: 5.0

      team-beta:
        api_key_ids:
          - sk-proj-ghi789
        max_dollars_per_hour: 10.0

    alerts:
      stdout: true
      webhook_url: null

    export:
      format: csv
      output_dir: ./reports
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_CONFIG_PATHS = ("watchdog.yaml", "watchdog.yml")


@dataclass
class KeyGroup:
    """A named group of API keys with an optional spend limit."""

    name: str
    api_key_ids: list[str] = field(default_factory=list)
    max_dollars_per_hour: Optional[float] = None


@dataclass
class AlertSettings:
    """How/where to send alerts when thresholds are breached."""

    stdout: bool = True
    webhook_url: Optional[str] = None


@dataclass
class ExportSettings:
    """Report export preferences."""

    format: str = "csv"  # "csv" or "json"
    output_dir: str = "./reports"


@dataclass
class WatchdogConfig:
    """Top-level configuration."""

    admin_key_env: str = "OPENAI_ADMIN_KEY"
    key_groups: dict[str, KeyGroup] = field(default_factory=dict)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    export: ExportSettings = field(default_factory=ExportSettings)

    def get_admin_key(self) -> Optional[str]:
        """Resolve the admin key from the configured environment variable."""
        return os.environ.get(self.admin_key_env)


def load_config(path: Optional[str] = None) -> WatchdogConfig:
    """Load configuration from a YAML file.

    If *path* is ``None``, searches for ``watchdog.yaml`` / ``watchdog.yml``
    in the current directory.  Returns a default config if no file is found.
    """
    if path is not None:
        config_path = Path(path)
        if not config_path.is_file():
            raise FileNotFoundError(f"Config file not found: {path}")
    else:
        config_path = None
        for candidate in DEFAULT_CONFIG_PATHS:
            p = Path(candidate)
            if p.is_file():
                config_path = p
                break
        if config_path is None:
            return WatchdogConfig()

    raw: dict[str, Any] = yaml.safe_load(config_path.read_text()) or {}
    return _parse_config(raw)


def _parse_config(raw: dict[str, Any]) -> WatchdogConfig:
    cfg = WatchdogConfig()

    cfg.admin_key_env = raw.get("admin_key_env", cfg.admin_key_env)

    # Key groups
    for name, group_raw in raw.get("key_groups", {}).items():
        if not isinstance(group_raw, dict):
            raise ValueError(f"key_groups.{name} must be a mapping")
        cfg.key_groups[name] = KeyGroup(
            name=name,
            api_key_ids=group_raw.get("api_key_ids", []),
            max_dollars_per_hour=group_raw.get("max_dollars_per_hour"),
        )

    # Alerts
    alerts_raw = raw.get("alerts", {})
    if isinstance(alerts_raw, dict):
        cfg.alerts = AlertSettings(
            stdout=alerts_raw.get("stdout", True),
            webhook_url=alerts_raw.get("webhook_url"),
        )

    # Export
    export_raw = raw.get("export", {})
    if isinstance(export_raw, dict):
        fmt = export_raw.get("format", "csv")
        if fmt not in ("csv", "json"):
            raise ValueError(f"export.format must be 'csv' or 'json', got {fmt!r}")
        cfg.export = ExportSettings(
            format=fmt,
            output_dir=export_raw.get("output_dir", "./reports"),
        )

    return cfg
