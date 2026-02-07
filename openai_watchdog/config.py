"""YAML configuration for openai-watchdog.

Loads and validates a ``watchdog.yaml`` file that defines key groups,
rate-limit thresholds, alert settings, and export preferences.

Example config::

    admin_key_env: OPENAI_ADMIN_KEY

    poll:
      interval_seconds: 600       # 10 minutes
      db_path: watchdog.db

    key_groups:
      team-alpha:
        api_key_ids:
          - sk-proj-abc123
          - sk-proj-def456
        owner_email: alice@example.com
        limits:
          hourly: 20.0            # soft limit $/h
          daily: 30.0             # soft limit $/day
          hard_multiplier: 1.5    # hard = soft * 1.5

      team-beta:
        api_key_ids:
          - sk-proj-ghi789
        owner_email: bob@example.com
        limits:
          hourly: 10.0
          daily: 50.0

    alerts:
      stdout: true
      webhook_url: null
      email:
        smtp_host: smtp.example.com
        smtp_port: 587
        from_addr: watchdog@example.com
        username: watchdog@example.com
        password_env: SMTP_PASSWORD
        cc_addrs:
          - manager@example.com
          - billing@example.com

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

# Hard limit defaults to 150% of the soft limit.
DEFAULT_HARD_MULTIPLIER = 1.5


@dataclass
class SpendLimits:
    """Dollar spend limits for a key group."""

    hourly: Optional[float] = None          # soft limit $/h
    daily: Optional[float] = None           # soft limit $/day
    hard_multiplier: float = DEFAULT_HARD_MULTIPLIER  # hard = soft * this

    @property
    def hard_hourly(self) -> Optional[float]:
        return self.hourly * self.hard_multiplier if self.hourly is not None else None

    @property
    def hard_daily(self) -> Optional[float]:
        return self.daily * self.hard_multiplier if self.daily is not None else None

    @property
    def has_any_limit(self) -> bool:
        return self.hourly is not None or self.daily is not None


@dataclass
class KeyGroup:
    """A named group of API keys with spend limits."""

    name: str
    api_key_ids: list[str] = field(default_factory=list)
    owner_email: Optional[str] = None
    limits: SpendLimits = field(default_factory=SpendLimits)
    default: bool = False  # If True, catches all keys not in other groups

    # Back-compat alias
    @property
    def max_dollars_per_hour(self) -> Optional[float]:
        return self.limits.hourly


@dataclass
class EmailSettings:
    """SMTP configuration for sending alert emails."""

    smtp_host: str = ""
    smtp_port: int = 587
    from_addr: str = ""
    username: str = ""
    password_env: str = "SMTP_PASSWORD"
    cc_addrs: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.smtp_host and self.from_addr)

    def get_password(self) -> Optional[str]:
        return os.environ.get(self.password_env)


@dataclass
class AlertSettings:
    """How/where to send alerts when thresholds are breached."""

    stdout: bool = True
    webhook_url: Optional[str] = None
    email: EmailSettings = field(default_factory=EmailSettings)


@dataclass
class PollSettings:
    """Poller configuration."""

    interval_seconds: int = 600   # 10 minutes
    db_path: str = "watchdog.db"


@dataclass
class ExportSettings:
    """Report export preferences."""

    format: str = "csv"  # "csv" or "json"
    output_dir: str = "./reports"


@dataclass
class WatchdogConfig:
    """Top-level configuration."""

    admin_key_env: str = "OPENAI_ADMIN_KEY"
    has_org_admin: bool = False  # If True, key can list/disable API keys
    poll: PollSettings = field(default_factory=PollSettings)
    key_groups: dict[str, KeyGroup] = field(default_factory=dict)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    export: ExportSettings = field(default_factory=ExportSettings)

    def get_admin_key(self) -> Optional[str]:
        """Resolve the admin key from the configured environment variable."""
        return os.environ.get(self.admin_key_env)


def resolve_default_groups(cfg: WatchdogConfig, all_key_ids: list[str]) -> None:
    """Populate api_key_ids for groups marked as default.

    For each group with ``default=True``, sets its ``api_key_ids`` to all keys
    in *all_key_ids* that are not explicitly listed in any other group.
    """
    # Collect all explicitly-assigned key IDs
    explicit_keys: set[str] = set()
    for group in cfg.key_groups.values():
        if not group.default:
            explicit_keys.update(group.api_key_ids)

    # Assign remaining keys to default groups
    remaining_keys = [k for k in all_key_ids if k not in explicit_keys]
    for group in cfg.key_groups.values():
        if group.default:
            group.api_key_ids = remaining_keys


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
    cfg.has_org_admin = raw.get("has_org_admin", False)

    # Poll settings
    poll_raw = raw.get("poll", {})
    if isinstance(poll_raw, dict):
        cfg.poll = PollSettings(
            interval_seconds=poll_raw.get("interval_seconds", 600),
            db_path=poll_raw.get("db_path", "watchdog.db"),
        )

    # Key groups
    for name, group_raw in raw.get("key_groups", {}).items():
        if not isinstance(group_raw, dict):
            raise ValueError(f"key_groups.{name} must be a mapping")

        limits = SpendLimits()
        limits_raw = group_raw.get("limits", {})
        if isinstance(limits_raw, dict):
            limits = SpendLimits(
                hourly=limits_raw.get("hourly"),
                daily=limits_raw.get("daily"),
                hard_multiplier=limits_raw.get(
                    "hard_multiplier", DEFAULT_HARD_MULTIPLIER
                ),
            )
        # Back-compat: old max_dollars_per_hour field
        elif "max_dollars_per_hour" in group_raw:
            limits = SpendLimits(hourly=group_raw["max_dollars_per_hour"])

        cfg.key_groups[name] = KeyGroup(
            name=name,
            api_key_ids=group_raw.get("api_key_ids", []),
            owner_email=group_raw.get("owner_email"),
            limits=limits,
            default=group_raw.get("default", False),
        )

    # Alerts
    alerts_raw = raw.get("alerts", {})
    if isinstance(alerts_raw, dict):
        email = EmailSettings()
        email_raw = alerts_raw.get("email", {})
        if isinstance(email_raw, dict):
            cc_raw = email_raw.get("cc_addrs", [])
            if isinstance(cc_raw, str):
                cc_raw = [cc_raw]
            email = EmailSettings(
                smtp_host=email_raw.get("smtp_host", ""),
                smtp_port=email_raw.get("smtp_port", 587),
                from_addr=email_raw.get("from_addr", ""),
                username=email_raw.get("username", ""),
                password_env=email_raw.get("password_env", "SMTP_PASSWORD"),
                cc_addrs=cc_raw,
            )
        cfg.alerts = AlertSettings(
            stdout=alerts_raw.get("stdout", True),
            webhook_url=alerts_raw.get("webhook_url"),
            email=email,
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
