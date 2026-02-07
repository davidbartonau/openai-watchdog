"""Periodic usage poller.

Designed to be called every N minutes (default 10), either from cron
(``openai-watchdog poll --once``) or as a long-running daemon
(``openai-watchdog poll``).

Each invocation:

1. Determines the time window: ``last_poll_ts .. now``.
   On first run (no history), looks back 24 hours to seed rolling windows.
2. Fetches usage from the OpenAI API for that window.
3. Stores per-group and per-key dollar costs in SQLite.
4. Queries rolling 1-hour and 24-hour totals from the DB.
5. Evaluates soft and hard limits, triggers enforcement actions.
6. Prints the 5 most expensive keys in the last 24 hours.

All timestamps are UTC.
"""

import sys
import time
from datetime import datetime, timezone

from openai_watchdog.config import WatchdogConfig, KeyGroup
from openai_watchdog.db import WatchdogDB
from openai_watchdog.monitor import aggregate_group_usage
from openai_watchdog.usage import UsageClient


def _utc_label(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def run_poll_cycle(
    cfg: WatchdogConfig,
    client: UsageClient,
    db: WatchdogDB,
    *,
    verbose: bool = False,
    show_top: int = 5,
    has_org_admin: bool = False,
) -> list[dict]:
    """Execute one poll cycle.  Returns a list of limit-check results.

    Each result dict has (one per key, not per group)::

        {
            "key_id": str,
            "group": str,
            "hourly_cost": float,
            "daily_cost": float,
            "hourly_limit": float,
            "daily_limit": float,
            "hard_hourly_limit": float,
            "hard_daily_limit": float,
            "soft_hourly_exceeded": bool,
            "soft_daily_exceeded": bool,
            "hard_hourly_exceeded": bool,
            "hard_daily_exceeded": bool,
        }
    """
    from openai_watchdog.enforcement import enforce_limits

    poll_start = db.record_poll_start()
    now = int(time.time())

    # Figure out the window to query.
    last_poll = db.last_successful_poll_ts()
    first_run = last_poll is None
    if last_poll is not None:
        # Query from last successful poll to now.
        query_start = last_poll
    else:
        # First run — look back 24 hours to seed the daily rolling window.
        # Hourly limits are skipped on first run since we don't have
        # granular per-interval data yet (see _evaluate_limits).
        query_start = now - 86400

    if verbose:
        print(f"[poll] {_utc_label(now)}")
        if first_run:
            print(f"[poll] first run — seeding with 24h lookback")
        print(f"[poll] window: {_utc_label(query_start)} -> {_utc_label(now)}  "
              f"({(now - query_start) / 60:.0f} min)")

    # Fetch and store usage for each group.
    for group in cfg.key_groups.values():
        if not group.api_key_ids:
            continue
        try:
            summary = aggregate_group_usage(client, group, query_start, now)
            cost = summary.estimated_cost_usd
        except Exception as exc:
            if verbose:
                print(f"[poll] ERROR fetching {group.name}: {exc}",
                      file=sys.stderr)
            cost = 0.0
            summary = None

        db.store_usage(
            group_name=group.name,
            poll_ts=now,
            period_start=query_start,
            period_end=now,
            cost_usd=cost,
        )

        # Store per-key costs for the top-keys report.
        if summary and summary.key_costs:
            for key_id, key_cost in summary.key_costs.items():
                db.store_key_cost(key_id, group.name, now, key_cost)

        if verbose:
            print(f"[poll] {group.name}: ${cost:.6f} in this interval")

    db.record_poll_end(poll_start)

    # Print usage summary
    if verbose:
        interval_total = db.interval_cost(now)
        hourly_total = db.total_rolling_cost(3600)
        daily_total = db.total_rolling_cost(86400)
        print(f"[poll] ── Usage Summary {'─' * 48}")
        print(f"[poll]   This interval: ${interval_total:.4f}")
        print(f"[poll]   Last hour:     ${hourly_total:.4f}")
        print(f"[poll]   Last 24h:      ${daily_total:.4f}")
        print(f"[poll] {'─' * 70}")

    # Now evaluate rolling limits from the DB — per key, not per group.
    # Groups define the limits, but each key is checked individually.
    results = []
    for group in cfg.key_groups.values():
        if not group.limits.has_any_limit:
            continue

        for key_id in group.api_key_ids:
            hourly = db.rolling_key_cost_hourly(key_id)
            daily = db.rolling_key_cost_daily(key_id)

            # On first run we only have one big 24h chunk — hourly limits
            # are meaningless because the entire day's cost is stuffed into
            # a single record.  Only check daily limits until we have
            # granular per-interval data.
            result = _evaluate_key_limits(
                key_id, group, hourly, daily, skip_hourly=first_run
            )
            results.append(result)

    # Print group summaries (aggregate for display only)
    if verbose:
        _print_group_summaries(results, cfg, skip_hourly=first_run)

    # Print top N most expensive keys (last 24h).
    if verbose and show_top > 0:
        _print_top_keys(db, client, show_top, has_org_admin)

    # Run enforcement (emails, key restriction).
    enforce_limits(results, cfg, db, client, verbose=verbose)

    # Prune old data (keep 7 days).
    db.prune_old_records()

    return results


def _evaluate_key_limits(
    key_id: str,
    group: KeyGroup,
    hourly_cost: float,
    daily_cost: float,
    *,
    skip_hourly: bool = False,
) -> dict:
    """Evaluate limits for a single API key against its group's limits."""
    limits = group.limits
    result = {
        "key_id": key_id,
        "group": group.name,
        "hourly_cost": hourly_cost,
        "daily_cost": daily_cost,
        "hourly_limit": limits.hourly,
        "daily_limit": limits.daily,
        "hard_hourly_limit": limits.hard_hourly,
        "hard_daily_limit": limits.hard_daily,
        "soft_hourly_exceeded": False,
        "soft_daily_exceeded": False,
        "hard_hourly_exceeded": False,
        "hard_daily_exceeded": False,
    }

    if limits.hourly is not None and not skip_hourly:
        result["soft_hourly_exceeded"] = hourly_cost > limits.hourly
        result["hard_hourly_exceeded"] = hourly_cost > limits.hard_hourly

    if limits.daily is not None:
        result["soft_daily_exceeded"] = daily_cost > limits.daily
        result["hard_daily_exceeded"] = daily_cost > limits.hard_daily

    return result


def _print_group_summaries(
    results: list[dict],
    cfg: WatchdogConfig,
    *,
    skip_hourly: bool = False,
) -> None:
    """Print per-group summary with key-level status indicators."""
    # Group results by group name
    by_group: dict[str, list[dict]] = {}
    for r in results:
        by_group.setdefault(r["group"], []).append(r)

    for group_name, group_results in by_group.items():
        group = cfg.key_groups.get(group_name)
        if not group:
            continue

        # Aggregate costs for display
        total_hourly = sum(r["hourly_cost"] for r in group_results)
        total_daily = sum(r["daily_cost"] for r in group_results)

        # Count keys exceeding limits
        soft_hourly_count = sum(1 for r in group_results if r["soft_hourly_exceeded"])
        soft_daily_count = sum(1 for r in group_results if r["soft_daily_exceeded"])
        hard_hourly_count = sum(1 for r in group_results if r["hard_hourly_exceeded"])
        hard_daily_count = sum(1 for r in group_results if r["hard_daily_exceeded"])

        flags = []
        if soft_hourly_count:
            flags.append(f"SOFT-1H:{soft_hourly_count}keys")
        if soft_daily_count:
            flags.append(f"SOFT-24H:{soft_daily_count}keys")
        if hard_hourly_count:
            flags.append(f"HARD-1H:{hard_hourly_count}keys")
        if hard_daily_count:
            flags.append(f"HARD-24H:{hard_daily_count}keys")

        status = ", ".join(flags) if flags else "OK"
        h_part = (
            f"1h=${total_hourly:.4f}"
            f"{'/' + f'${group.limits.hourly:.2f}' if group.limits.hourly else ''}"
            if not skip_hourly else "1h=n/a (first run)"
        )
        print(
            f"[poll] {group_name} ({len(group_results)} keys):  "
            f"{h_part}  "
            f"24h=${total_daily:.4f}"
            f"{'/' + f'${group.limits.daily:.2f}' if group.limits.daily else ''}  "
            f"[{status}]"
        )


def _print_top_keys(db: WatchdogDB, client: UsageClient, limit: int = 5, has_org_admin: bool = False) -> None:
    """Print the N most expensive API keys in the last 24 hours."""
    top = db.top_keys_by_cost(window_seconds=86400, limit=limit)
    if not top:
        return
    print(f"[poll] Top {limit} keys by cost (24h):")

    if has_org_admin:
        print(f"[poll]   {'#':<3s} {'Key ID':<25s} {'Name':<22s} {'Owner':<28s} {'Cost':>10s}")
        print(f"[poll]   {'-'*3} {'-'*25} {'-'*22} {'-'*28} {'-'*10}")
    else:
        print(f"[poll]   {'#':<3s} {'Key ID':<25s} {'Cost':>10s}")
        print(f"[poll]   {'-'*3} {'-'*25} {'-'*10}")

    for i, (key_id, group_name, cost) in enumerate(top, 1):
        # Truncate key ID for display (show first 12 + last 4 chars).
        if len(key_id) > 23:
            display_key = key_id[:12] + "..." + key_id[-6:]
        else:
            display_key = key_id

        if has_org_admin:
            # Get key metadata from cache or API
            info = client.get_key_info(key_id)
            name = info.name[:20] + ".." if len(info.name) > 22 else info.name
            owner = info.owner_email or info.owner_name or "-"
            owner = owner[:26] + ".." if len(owner) > 28 else owner
            print(f"[poll]   {i:<3d} {display_key:<25s} {name:<22s} {owner:<28s} ${cost:>9.4f}")
        else:
            print(f"[poll]   {i:<3d} {display_key:<25s} ${cost:>9.4f}")
