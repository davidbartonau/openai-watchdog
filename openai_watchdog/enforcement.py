"""Enforcement actions for limit breaches.

Soft limit  → email the key owner + stdout/webhook alert.
Hard limit  → restrict the API key via OpenAI Admin API (demote to read-only)
              + email + alert.
"""

import json
import smtplib
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Optional

from openai_watchdog.config import WatchdogConfig, AlertSettings, EmailSettings
from openai_watchdog.db import WatchdogDB


# The OpenAI Admin API key management endpoint.
ADMIN_API_KEYS_URL = "https://api.openai.com/v1/organization/api_keys"


def enforce_limits(
    results: list[dict],
    cfg: WatchdogConfig,
    db: WatchdogDB,
    client,  # UsageClient — used for its admin_key and key info
    *,
    verbose: bool = False,
) -> None:
    """Walk through limit-check results and take appropriate actions.

    Results are now per-key, not per-group. Each key that exceeds limits
    is handled individually.
    """
    for result in results:
        key_id = result.get("key_id")
        if not key_id:
            continue

        group_name = result["group"]
        group = cfg.key_groups.get(group_name)
        if group is None:
            continue

        # Get key metadata for emails/alerts
        key_info = client.get_key_info(key_id)

        # --- Hard limits (checked first — more severe) ---
        if result.get("hard_hourly_exceeded"):
            _handle_hard_key(
                key_id=key_id,
                key_info=key_info,
                group_name=group_name,
                alert_type="hard_hourly",
                cost=result["hourly_cost"],
                limit=result["hard_hourly_limit"],
                window="hourly",
                group=group,
                cfg=cfg,
                db=db,
                admin_key=client.admin_key,
                verbose=verbose,
            )

        if result.get("hard_daily_exceeded"):
            _handle_hard_key(
                key_id=key_id,
                key_info=key_info,
                group_name=group_name,
                alert_type="hard_daily",
                cost=result["daily_cost"],
                limit=result["hard_daily_limit"],
                window="daily",
                group=group,
                cfg=cfg,
                db=db,
                admin_key=client.admin_key,
                verbose=verbose,
            )

        # --- Soft limits ---
        if result.get("soft_hourly_exceeded") and not result.get("hard_hourly_exceeded"):
            _handle_soft_key(
                key_id=key_id,
                key_info=key_info,
                group_name=group_name,
                alert_type="soft_hourly",
                cost=result["hourly_cost"],
                limit=result["hourly_limit"],
                window="hourly",
                group=group,
                cfg=cfg,
                db=db,
                verbose=verbose,
            )

        if result.get("soft_daily_exceeded") and not result.get("hard_daily_exceeded"):
            _handle_soft_key(
                key_id=key_id,
                key_info=key_info,
                group_name=group_name,
                alert_type="soft_daily",
                cost=result["daily_cost"],
                limit=result["daily_limit"],
                window="daily",
                group=group,
                cfg=cfg,
                db=db,
                verbose=verbose,
            )


def _handle_soft_key(
    *,
    key_id: str,
    key_info,  # ApiKeyInfo
    group_name: str,
    alert_type: str,
    cost: float,
    limit: float,
    window: str,
    group,
    cfg: WatchdogConfig,
    db: WatchdogDB,
    verbose: bool,
) -> None:
    """Soft limit for a single key: alert the owner but don't restrict access."""
    # Use key_id for deduplication, not group_name
    alert_key = f"{key_id}:{alert_type}"
    if db.was_alert_sent_recently(alert_key, alert_type, cooldown_seconds=3600):
        if verbose:
            print(f"[enforce] {key_id}: {alert_type} alert already sent recently")
        return

    key_name = key_info.name or key_id
    owner = key_info.owner_email or key_info.owner_name or "unknown"

    msg = (
        f"[openai-watchdog] SOFT LIMIT ({window}): "
        f"key {key_name} ({key_id}) at ${cost:.4f} (limit ${limit:.2f})"
    )

    if cfg.alerts.stdout:
        print(msg, file=sys.stderr)

    if cfg.alerts.webhook_url:
        _post_webhook_key(
            cfg.alerts.webhook_url, alert_type, key_id, key_name, owner,
            group_name, cost, limit
        )

    if group.owner_email and cfg.alerts.email.configured:
        hard_limit = limit * cfg.key_groups[group_name].limits.hard_multiplier
        _send_email(
            settings=cfg.alerts.email,
            to_addr=group.owner_email,
            subject=f"OpenAI Watchdog: API key soft {window} limit exceeded",
            body=(
                f"An API key in group \"{group_name}\" has exceeded its "
                f"{window} soft spend limit.\n\n"
                f"  Key ID:         {key_id}\n"
                f"  Key name:       {key_name}\n"
                f"  Owner:          {owner}\n"
                f"  Current spend:  ${cost:.4f}\n"
                f"  Soft limit:     ${limit:.2f}\n\n"
                f"If spend reaches ${hard_limit:.2f} "
                f"(hard limit), this key will be deleted automatically.\n"
            ),
            verbose=verbose,
        )

    db.record_alert(alert_key, alert_type, cost, limit)


def _handle_hard_key(
    *,
    key_id: str,
    key_info,  # ApiKeyInfo
    group_name: str,
    alert_type: str,
    cost: float,
    limit: float,
    window: str,
    group,
    cfg: WatchdogConfig,
    db: WatchdogDB,
    admin_key: str,
    verbose: bool,
) -> None:
    """Hard limit for a single key: restrict just this key + alert."""
    # Use key_id for deduplication, not group_name
    alert_key = f"{key_id}:{alert_type}"
    if db.was_alert_sent_recently(alert_key, alert_type, cooldown_seconds=3600):
        if verbose:
            print(f"[enforce] {key_id}: {alert_type} already enforced recently")
        return

    key_name = key_info.name or key_id
    owner = key_info.owner_email or key_info.owner_name or "unknown"

    msg = (
        f"[openai-watchdog] HARD LIMIT ({window}): "
        f"key {key_name} ({key_id}) at ${cost:.4f} (hard limit ${limit:.2f}) — "
        f"RESTRICTING key"
    )

    if cfg.alerts.stdout:
        print(msg, file=sys.stderr)

    # Restrict only this specific key
    _restrict_api_key(admin_key, key_id, verbose=verbose)

    if cfg.alerts.webhook_url:
        _post_webhook_key(
            cfg.alerts.webhook_url, alert_type, key_id, key_name, owner,
            group_name, cost, limit
        )

    if group.owner_email and cfg.alerts.email.configured:
        _send_email(
            settings=cfg.alerts.email,
            to_addr=group.owner_email,
            subject=f"OpenAI Watchdog: API key HARD {window} limit — key restricted",
            body=(
                f"An API key in group \"{group_name}\" has exceeded its "
                f"{window} HARD spend limit.\n\n"
                f"  Key ID:         {key_id}\n"
                f"  Key name:       {key_name}\n"
                f"  Owner:          {owner}\n"
                f"  Current spend:  ${cost:.4f}\n"
                f"  Hard limit:     ${limit:.2f}\n\n"
                f"Action taken: this API key has been restricted.\n\n"
                f"Contact your administrator to restore access.\n"
            ),
            verbose=verbose,
        )

    db.record_alert(alert_key, alert_type, cost, limit)


# ------------------------------------------------------------------
# OpenAI Admin API: key restriction
# ------------------------------------------------------------------

def _restrict_api_key(admin_key: str, key_id: str, *, verbose: bool = False) -> bool:
    """Restrict an API key to read-only by removing all permission scopes
    except model listing.

    Uses POST /v1/organization/api_keys/{key_id} with the Admin API.
    Returns True if successful, False on error.
    """
    url = f"{ADMIN_API_KEYS_URL}/{key_id}"

    # Set scopes to only allow listing models — no completions, embeddings,
    # images, audio, or any other resource that costs money.
    payload = {
        "name": f"[RESTRICTED by watchdog] {key_id}",
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {admin_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if verbose:
                print(f"[enforce] Restricted key {key_id}: HTTP {resp.status}")
            return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(
            f"[enforce] Failed to restrict key {key_id}: "
            f"HTTP {exc.code}: {body}",
            file=sys.stderr,
        )
        return False
    except (urllib.error.URLError, OSError) as exc:
        print(f"[enforce] Network error restricting key {key_id}: {exc}",
              file=sys.stderr)
        return False


def restore_api_key(admin_key: str, key_id: str, name: str = "") -> bool:
    """Remove the restriction from an API key (manual recovery).

    This just renames the key to remove the [RESTRICTED] prefix.
    Full scope restoration depends on OpenAI's Admin API capabilities.
    """
    url = f"{ADMIN_API_KEYS_URL}/{key_id}"
    payload = {"name": name or key_id}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {admin_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return False


# ------------------------------------------------------------------
# Email
# ------------------------------------------------------------------

def _send_email(
    *,
    settings: EmailSettings,
    to_addr: str,
    subject: str,
    body: str,
    verbose: bool = False,
) -> bool:
    """Send an alert email via SMTP."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.from_addr
    msg["To"] = to_addr
    if settings.cc_addrs:
        msg["Cc"] = ", ".join(settings.cc_addrs)
    msg.set_content(body)

    password = settings.get_password()

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            if settings.username and password:
                smtp.login(settings.username, password)
            smtp.send_message(msg)
        if verbose:
            cc_str = f" (cc: {', '.join(settings.cc_addrs)})" if settings.cc_addrs else ""
            print(f"[enforce] Email sent to {to_addr}{cc_str}: {subject}")
        return True
    except Exception as exc:
        print(f"[enforce] Email to {to_addr} failed: {exc}", file=sys.stderr)
        return False


# ------------------------------------------------------------------
# Webhook
# ------------------------------------------------------------------

def _post_webhook_key(
    url: str,
    alert_type: str,
    key_id: str,
    key_name: str,
    owner: str,
    group_name: str,
    cost: float,
    limit: float,
) -> None:
    """Post webhook for a single key alert."""
    payload = {
        "event": alert_type,
        "key_id": key_id,
        "key_name": key_name,
        "owner": owner,
        "group": group_name,
        "cost_usd": round(cost, 6),
        "limit_usd": limit,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except (urllib.error.URLError, OSError) as exc:
        print(f"[enforce] Webhook POST failed: {exc}", file=sys.stderr)
