"""Enforcement actions for limit breaches.

Soft limit  → email the key owner + stdout/webhook alert.
Hard limit  → DELETE the API key via OpenAI Admin API + email + alert.
"""

import json
import smtplib
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Optional

from openai_watchdog.config import WatchdogConfig, AlertSettings, EmailSettings
from openai_watchdog.db import WatchdogDB


# The OpenAI Admin API key management endpoint.
ADMIN_API_KEYS_URL = "https://api.openai.com/v1/organization/api_keys"


@dataclass
class KeyAlert:
    """Details about a key that exceeded a limit."""
    key_id: str
    key_name: str
    owner: str
    group_name: str
    alert_type: str  # soft_hourly, soft_daily, hard_hourly, hard_daily
    cost: float
    limit: float
    hard_limit: float  # For soft alerts, shows when key will be deleted


def enforce_limits(
    results: list[dict],
    cfg: WatchdogConfig,
    db: WatchdogDB,
    client,  # UsageClient — used for its admin_key and key info
    *,
    verbose: bool = False,
) -> None:
    """Walk through limit-check results and take appropriate actions.

    Results are per-key. This function:
    1. Collects all over-limit keys
    2. Performs enforcement actions (delete for hard limits)
    3. Sends batched emails with all over-limit keys listed
    4. Posts webhooks for each key individually
    """
    # Collect all alerts, grouped by owner email for batched emails
    soft_alerts: list[KeyAlert] = []
    hard_alerts: list[KeyAlert] = []

    for result in results:
        key_id = result.get("key_id")
        if not key_id:
            continue

        group_name = result["group"]
        group = cfg.key_groups.get(group_name)
        if group is None:
            continue

        # Get key metadata
        key_info = client.get_key_info(key_id)
        key_name = key_info.name or key_id
        owner = key_info.owner_email or key_info.owner_name or "unknown"

        # --- Hard limits (checked first — more severe) ---
        if result.get("hard_hourly_exceeded"):
            alert = KeyAlert(
                key_id=key_id,
                key_name=key_name,
                owner=owner,
                group_name=group_name,
                alert_type="hard_hourly",
                cost=result["hourly_cost"],
                limit=result["hard_hourly_limit"],
                hard_limit=result["hard_hourly_limit"],
            )
            hard_alerts.append(alert)

        if result.get("hard_daily_exceeded"):
            alert = KeyAlert(
                key_id=key_id,
                key_name=key_name,
                owner=owner,
                group_name=group_name,
                alert_type="hard_daily",
                cost=result["daily_cost"],
                limit=result["hard_daily_limit"],
                hard_limit=result["hard_daily_limit"],
            )
            hard_alerts.append(alert)

        # --- Soft limits (only if not already at hard limit) ---
        if result.get("soft_hourly_exceeded") and not result.get("hard_hourly_exceeded"):
            alert = KeyAlert(
                key_id=key_id,
                key_name=key_name,
                owner=owner,
                group_name=group_name,
                alert_type="soft_hourly",
                cost=result["hourly_cost"],
                limit=result["hourly_limit"],
                hard_limit=result["hard_hourly_limit"],
            )
            soft_alerts.append(alert)

        if result.get("soft_daily_exceeded") and not result.get("hard_daily_exceeded"):
            alert = KeyAlert(
                key_id=key_id,
                key_name=key_name,
                owner=owner,
                group_name=group_name,
                alert_type="soft_daily",
                cost=result["daily_cost"],
                limit=result["daily_limit"],
                hard_limit=result["hard_daily_limit"],
            )
            soft_alerts.append(alert)

    # Process hard alerts first (delete keys, send emails)
    _process_hard_alerts(hard_alerts, cfg, db, client.admin_key, verbose)

    # Process soft alerts (warnings only)
    _process_soft_alerts(soft_alerts, cfg, db, verbose)


def _process_hard_alerts(
    alerts: list[KeyAlert],
    cfg: WatchdogConfig,
    db: WatchdogDB,
    admin_key: str,
    verbose: bool,
) -> None:
    """Process hard limit alerts: delete keys and send batched email."""
    if not alerts:
        return

    cooldown = cfg.alerts.email.cooldown_seconds

    # Filter out alerts that were already sent recently
    new_alerts = []
    for alert in alerts:
        alert_key = f"{alert.key_id}:{alert.alert_type}"
        if db.was_alert_sent_recently(alert_key, alert.alert_type, cooldown_seconds=cooldown):
            if verbose:
                print(f"[enforce] {alert.key_id}: {alert.alert_type} already enforced recently")
            continue
        new_alerts.append(alert)

    if not new_alerts:
        return

    # Print to stdout and delete each key
    for alert in new_alerts:
        window = "hourly" if "hourly" in alert.alert_type else "daily"
        msg = (
            f"[openai-watchdog] HARD LIMIT ({window}): "
            f"key {alert.key_name} ({alert.key_id}) at ${alert.cost:.4f} "
            f"(hard limit ${alert.limit:.2f}) — DELETING key"
        )
        if cfg.alerts.stdout:
            print(msg, file=sys.stderr)

        # Delete the key
        _delete_api_key(admin_key, alert.key_id, verbose=verbose)

        # Post webhook
        if cfg.alerts.webhook_url:
            _post_webhook_key(
                cfg.alerts.webhook_url, alert.alert_type, alert.key_id,
                alert.key_name, alert.owner, alert.group_name,
                alert.cost, alert.limit
            )

        # Record alert
        alert_key = f"{alert.key_id}:{alert.alert_type}"
        db.record_alert(alert_key, alert.alert_type, alert.cost, alert.limit)

    # Send batched email with all hard limit keys (with smart cooldown)
    if cfg.alerts.email.configured:
        _send_batched_email(new_alerts, cfg, db, is_hard=True, verbose=verbose)


def _process_soft_alerts(
    alerts: list[KeyAlert],
    cfg: WatchdogConfig,
    db: WatchdogDB,
    verbose: bool,
) -> None:
    """Process soft limit alerts: send warnings."""
    if not alerts:
        return

    cooldown = cfg.alerts.email.cooldown_seconds

    # Filter out alerts that were already sent recently
    new_alerts = []
    for alert in alerts:
        alert_key = f"{alert.key_id}:{alert.alert_type}"
        if db.was_alert_sent_recently(alert_key, alert.alert_type, cooldown_seconds=cooldown):
            if verbose:
                print(f"[enforce] {alert.key_id}: {alert.alert_type} alert already sent recently")
            continue
        new_alerts.append(alert)

    if not new_alerts:
        return

    # Print to stdout for each key
    for alert in new_alerts:
        window = "hourly" if "hourly" in alert.alert_type else "daily"
        msg = (
            f"[openai-watchdog] SOFT LIMIT ({window}): "
            f"key {alert.key_name} ({alert.key_id}) at ${alert.cost:.4f} "
            f"(limit ${alert.limit:.2f})"
        )
        if cfg.alerts.stdout:
            print(msg, file=sys.stderr)

        # Post webhook
        if cfg.alerts.webhook_url:
            _post_webhook_key(
                cfg.alerts.webhook_url, alert.alert_type, alert.key_id,
                alert.key_name, alert.owner, alert.group_name,
                alert.cost, alert.limit
            )

        # Record alert
        alert_key = f"{alert.key_id}:{alert.alert_type}"
        db.record_alert(alert_key, alert.alert_type, alert.cost, alert.limit)

    # Send batched email with all soft limit keys (with smart cooldown)
    if cfg.alerts.email.configured:
        _send_batched_email(new_alerts, cfg, db, is_hard=False, verbose=verbose)


def _send_batched_email(
    alerts: list[KeyAlert],
    cfg: WatchdogConfig,
    db: WatchdogDB,
    *,
    is_hard: bool,
    verbose: bool,
) -> None:
    """Send a single email listing all over-limit keys.

    Uses smart cooldown: if all current keys were in the previous email
    batch within the cooldown period, skip sending the email.
    """
    if not alerts:
        return

    batch_type = "hard" if is_hard else "soft"
    cooldown = cfg.alerts.email.cooldown_seconds
    current_key_ids = [a.key_id for a in alerts]

    # Check if we should skip this email (smart cooldown)
    if db.should_skip_email(batch_type, current_key_ids, cooldown):
        if verbose:
            print(f"[enforce] Skipping {batch_type} email: all keys were in previous alert")
        return

    # Group alerts by owner_email (from the group config)
    alerts_by_owner: dict[str, list[KeyAlert]] = {}
    for alert in alerts:
        group = cfg.key_groups.get(alert.group_name)
        owner_email = group.owner_email if group else None
        if owner_email:
            alerts_by_owner.setdefault(owner_email, []).append(alert)

    # Send one email per owner
    for owner_email, owner_alerts in alerts_by_owner.items():
        if is_hard:
            subject = f"OpenAI Watchdog: {len(owner_alerts)} key(s) DELETED (hard limit)"
            intro = (
                f"{len(owner_alerts)} API key(s) have exceeded their HARD spend limits "
                f"and have been DELETED.\n\n"
            )
            action = "\nAction taken: These keys have been DELETED.\n"
            footer = "Contact your administrator to create new keys.\n"
        else:
            subject = f"OpenAI Watchdog: {len(owner_alerts)} key(s) over soft limit"
            intro = (
                f"{len(owner_alerts)} API key(s) have exceeded their soft spend limits.\n\n"
            )
            action = ""
            footer = (
                "\nWARNING: If any key reaches its hard limit, it will be "
                "DELETED automatically.\n"
            )

        # Build the key details table
        body = intro
        body += "Keys over limit:\n"
        body += "-" * 80 + "\n"
        body += f"{'Key ID':<25s} {'Name':<20s} {'Type':<12s} {'Cost':>10s} {'Limit':>10s}\n"
        body += "-" * 80 + "\n"

        for alert in owner_alerts:
            limit_type = alert.alert_type.replace("_", " ").title()
            body += (
                f"{alert.key_id:<25s} "
                f"{alert.key_name[:18]:<20s} "
                f"{limit_type:<12s} "
                f"${alert.cost:>9.4f} "
                f"${alert.limit:>9.2f}\n"
            )

        body += "-" * 80 + "\n"
        body += action
        body += footer

        _send_email(
            settings=cfg.alerts.email,
            to_addr=owner_email,
            subject=subject,
            body=body,
            verbose=verbose,
        )

    # Record this email batch for smart cooldown
    db.record_email_batch(batch_type, current_key_ids)


# ------------------------------------------------------------------
# OpenAI Admin API: key deletion
# ------------------------------------------------------------------

def _delete_api_key(admin_key: str, key_id: str, *, verbose: bool = False) -> bool:
    """Delete an API key when hard limit is breached.

    Uses DELETE /v1/organization/projects/{project_id}/api_keys/{key_id}
    with the Admin API. Returns True if successful, False on error.

    Note: OpenAI does not provide a way to disable keys - only delete them.
    """
    # The API key delete endpoint requires the project_id, but we may not have it.
    # Try deleting via the organization endpoint first (may work for admin keys).
    url = f"{ADMIN_API_KEYS_URL}/{key_id}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {admin_key}",
            "Content-Type": "application/json",
        },
        method="DELETE",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if verbose:
                print(f"[enforce] DELETED key {key_id}: HTTP {resp.status}")
            return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(
            f"[enforce] Failed to delete key {key_id}: "
            f"HTTP {exc.code}: {body}",
            file=sys.stderr,
        )
        return False
    except (urllib.error.URLError, OSError) as exc:
        print(f"[enforce] Network error deleting key {key_id}: {exc}",
              file=sys.stderr)
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
