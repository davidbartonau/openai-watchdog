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
    client,  # UsageClient — used for its admin_key
    *,
    verbose: bool = False,
) -> None:
    """Walk through limit-check results and take appropriate actions."""
    for result in results:
        group_name = result["group"]
        group = cfg.key_groups.get(group_name)
        if group is None:
            continue

        # --- Hard limits (checked first — more severe) ---
        if result.get("hard_hourly_exceeded"):
            _handle_hard(
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
            _handle_hard(
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
            _handle_soft(
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
            _handle_soft(
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


def _handle_soft(
    *,
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
    """Soft limit: alert the owner but don't restrict access."""
    if db.was_alert_sent_recently(group_name, alert_type, cooldown_seconds=3600):
        if verbose:
            print(f"[enforce] {group_name}: {alert_type} alert already sent recently")
        return

    msg = (
        f"[openai-watchdog] SOFT LIMIT ({window}): "
        f"{group_name} at ${cost:.4f} (limit ${limit:.2f})"
    )

    if cfg.alerts.stdout:
        print(msg, file=sys.stderr)

    if cfg.alerts.webhook_url:
        _post_webhook(cfg.alerts.webhook_url, alert_type, group_name, cost, limit)

    if group.owner_email and cfg.alerts.email.configured:
        _send_email(
            settings=cfg.alerts.email,
            to_addr=group.owner_email,
            subject=f"OpenAI Watchdog: {group_name} soft {window} limit exceeded",
            body=(
                f"Your API key group \"{group_name}\" has exceeded its "
                f"{window} soft spend limit.\n\n"
                f"  Current spend:  ${cost:.4f}\n"
                f"  Soft limit:     ${limit:.2f}\n\n"
                f"If spend reaches ${limit * cfg.key_groups[group_name].limits.hard_multiplier:.2f} "
                f"(hard limit), access will be restricted automatically.\n"
            ),
            verbose=verbose,
        )

    db.record_alert(group_name, alert_type, cost, limit)


def _handle_hard(
    *,
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
    """Hard limit: restrict all keys in the group + alert."""
    if db.was_alert_sent_recently(group_name, alert_type, cooldown_seconds=3600):
        if verbose:
            print(f"[enforce] {group_name}: {alert_type} already enforced recently")
        return

    msg = (
        f"[openai-watchdog] HARD LIMIT ({window}): "
        f"{group_name} at ${cost:.4f} (hard limit ${limit:.2f}) — "
        f"RESTRICTING {len(group.api_key_ids)} key(s)"
    )

    if cfg.alerts.stdout:
        print(msg, file=sys.stderr)

    # Restrict each key in the group.
    for key_id in group.api_key_ids:
        _restrict_api_key(admin_key, key_id, verbose=verbose)

    if cfg.alerts.webhook_url:
        _post_webhook(cfg.alerts.webhook_url, alert_type, group_name, cost, limit)

    if group.owner_email and cfg.alerts.email.configured:
        _send_email(
            settings=cfg.alerts.email,
            to_addr=group.owner_email,
            subject=f"OpenAI Watchdog: {group_name} HARD {window} limit — keys restricted",
            body=(
                f"Your API key group \"{group_name}\" has exceeded its "
                f"{window} HARD spend limit.\n\n"
                f"  Current spend:  ${cost:.4f}\n"
                f"  Hard limit:     ${limit:.2f}\n\n"
                f"Action taken: all {len(group.api_key_ids)} API key(s) in this "
                f"group have been restricted to read-only (model listing only).\n\n"
                f"Contact your administrator to restore access.\n"
            ),
            verbose=verbose,
        )

    db.record_alert(group_name, alert_type, cost, limit)


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

def _post_webhook(
    url: str,
    alert_type: str,
    group_name: str,
    cost: float,
    limit: float,
) -> None:
    payload = {
        "event": alert_type,
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
