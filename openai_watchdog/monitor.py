"""Key-group reporting, rate-limit monitoring, and alerting.

Ties together the UsageClient, pricing helpers, and WatchdogConfig to:
1. Aggregate usage per configured key group.
2. Compare recent spend against per-group $/h limits.
3. Fire alerts (stdout, webhook) when limits are breached.
"""

import json
import sys
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

from openai_watchdog.config import WatchdogConfig, KeyGroup, AlertSettings
from openai_watchdog.pricing import estimate_text_cost, estimate_embedding_cost
from openai_watchdog.usage import UsageClient, UsageResponse, hours_ago


@dataclass
class GroupUsageSummary:
    """Aggregated usage summary for a single key group."""

    group_name: str
    api_key_ids: list[str]
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int = 0
    total_requests: int = 0
    estimated_cost_usd: float = 0.0
    # Per-model breakdown: model -> estimated cost
    model_costs: dict[str, float] = field(default_factory=dict)


@dataclass
class RateLimitCheck:
    """Result of checking a group's spend against its $/h limit."""

    group_name: str
    max_dollars_per_hour: float
    actual_dollars_last_hour: float
    exceeded: bool = False
    pct_of_limit: float = 0.0


def aggregate_group_usage(
    client: UsageClient,
    group: KeyGroup,
    start_time: int,
    end_time: Optional[int] = None,
) -> GroupUsageSummary:
    """Fetch and aggregate usage for all API keys in *group*."""
    summary = GroupUsageSummary(
        group_name=group.name,
        api_key_ids=group.api_key_ids,
    )

    if not group.api_key_ids:
        return summary

    # Fetch completions (the primary cost driver) filtered to this group's keys
    try:
        resp = client.get_usage(
            "completions",
            start_time,
            end_time,
            bucket_width="1h",
            group_by=["api_key_id", "model"],
            api_key_ids=group.api_key_ids,
        )
        _accumulate_completions(summary, resp)
    except RuntimeError:
        pass

    # Fetch embeddings
    try:
        resp = client.get_usage(
            "embeddings",
            start_time,
            end_time,
            bucket_width="1h",
            group_by=["api_key_id", "model"],
            api_key_ids=group.api_key_ids,
        )
        _accumulate_embeddings(summary, resp)
    except RuntimeError:
        pass

    return summary


def _accumulate_completions(summary: GroupUsageSummary, resp: UsageResponse) -> None:
    for bucket in resp.data:
        for r in bucket.results:
            inp = r.get("input_tokens", 0) or 0
            out = r.get("output_tokens", 0) or 0
            cached = r.get("input_cached_tokens", 0) or 0
            reqs = r.get("num_model_requests", 0) or 0
            model = r.get("model", "unknown")

            summary.total_input_tokens += inp
            summary.total_output_tokens += out
            summary.total_cached_tokens += cached
            summary.total_requests += reqs

            cost = estimate_text_cost(model, inp, out, cached)
            if cost is not None:
                summary.estimated_cost_usd += cost
                summary.model_costs[model] = summary.model_costs.get(model, 0.0) + cost


def _accumulate_embeddings(summary: GroupUsageSummary, resp: UsageResponse) -> None:
    for bucket in resp.data:
        for r in bucket.results:
            inp = r.get("input_tokens", 0) or 0
            model = r.get("model", "unknown")

            summary.total_input_tokens += inp

            cost = estimate_embedding_cost(model, inp)
            if cost is not None:
                summary.estimated_cost_usd += cost
                summary.model_costs[model] = summary.model_costs.get(model, 0.0) + cost


# ------------------------------------------------------------------
# Rate-limit monitoring
# ------------------------------------------------------------------

def check_rate_limits(
    client: UsageClient,
    config: WatchdogConfig,
) -> list[RateLimitCheck]:
    """Check the last hour of spend for every group with a rate limit.

    Returns a list of RateLimitCheck results (one per group that has a
    ``max_dollars_per_hour`` configured).
    """
    results: list[RateLimitCheck] = []
    start = hours_ago(1)

    for group in config.key_groups.values():
        if group.max_dollars_per_hour is None:
            continue
        summary = aggregate_group_usage(client, group, start)
        exceeded = summary.estimated_cost_usd > group.max_dollars_per_hour
        pct = (
            (summary.estimated_cost_usd / group.max_dollars_per_hour * 100)
            if group.max_dollars_per_hour > 0
            else 0.0
        )
        results.append(RateLimitCheck(
            group_name=group.name,
            max_dollars_per_hour=group.max_dollars_per_hour,
            actual_dollars_last_hour=summary.estimated_cost_usd,
            exceeded=exceeded,
            pct_of_limit=pct,
        ))

    return results


# ------------------------------------------------------------------
# Alerting
# ------------------------------------------------------------------

def send_alerts(
    checks: list[RateLimitCheck],
    settings: AlertSettings,
) -> None:
    """Send alerts for any rate-limit breaches found in *checks*."""
    breaches = [c for c in checks if c.exceeded]
    if not breaches:
        return

    message = _format_alert_message(breaches)

    if settings.stdout:
        print(message, file=sys.stderr)

    if settings.webhook_url:
        _post_webhook(settings.webhook_url, breaches)


def _format_alert_message(breaches: list[RateLimitCheck]) -> str:
    lines = ["[openai-watchdog] RATE LIMIT EXCEEDED"]
    for b in breaches:
        lines.append(
            f"  {b.group_name}: ${b.actual_dollars_last_hour:.4f}/h "
            f"(limit ${b.max_dollars_per_hour:.2f}/h, {b.pct_of_limit:.0f}%)"
        )
    return "\n".join(lines)


def _post_webhook(url: str, breaches: list[RateLimitCheck]) -> None:
    """POST a JSON payload to a webhook URL."""
    payload = {
        "event": "rate_limit_exceeded",
        "breaches": [
            {
                "group": b.group_name,
                "actual_dollars_last_hour": round(b.actual_dollars_last_hour, 6),
                "max_dollars_per_hour": b.max_dollars_per_hour,
                "pct_of_limit": round(b.pct_of_limit, 1),
            }
            for b in breaches
        ],
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
        print(f"[openai-watchdog] webhook POST failed: {exc}", file=sys.stderr)
