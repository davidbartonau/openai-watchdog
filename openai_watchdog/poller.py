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
) -> list[dict]:
    """Execute one poll cycle.  Returns a list of limit-check results.

    Each result dict has::

        {
            "group": str,
            "hourly_cost": float,
            "daily_cost": float,
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

    # Now evaluate rolling limits from the DB.
    results = []
    for group in cfg.key_groups.values():
        if not group.limits.has_any_limit:
            continue

        hourly = db.rolling_cost_hourly(group.name)
        daily = db.rolling_cost_daily(group.name)

        # On first run we only have one big 24h chunk — hourly limits
        # are meaningless because the entire day's cost is stuffed into
        # a single record.  Only check daily limits until we have
        # granular per-interval data.
        result = _evaluate_limits(group, hourly, daily,
                                  skip_hourly=first_run)
        results.append(result)

        if verbose:
            _print_result(result, group, skip_hourly=first_run)

    # Print top 5 most expensive keys (last 24h).
    if verbose:
        _print_top_keys(db)

    # Run enforcement (emails, key restriction).
    enforce_limits(results, cfg, db, client, verbose=verbose)

    # Prune old data (keep 7 days).
    db.prune_old_records()

    return results


def _evaluate_limits(
    group: KeyGroup,
    hourly_cost: float,
    daily_cost: float,
    *,
    skip_hourly: bool = False,
) -> dict:
    limits = group.limits
    result = {
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


def _print_result(result: dict, group: KeyGroup, *, skip_hourly: bool = False) -> None:
    flags = []
    if result["soft_hourly_exceeded"]:
        flags.append("SOFT-HOURLY")
    if result["soft_daily_exceeded"]:
        flags.append("SOFT-DAILY")
    if result["hard_hourly_exceeded"]:
        flags.append("HARD-HOURLY")
    if result["hard_daily_exceeded"]:
        flags.append("HARD-DAILY")

    status = ", ".join(flags) if flags else "OK"
    h_part = (
        f"1h=${result['hourly_cost']:.4f}"
        f"{'/' + f'${group.limits.hourly:.2f}' if group.limits.hourly else ''}"
        if not skip_hourly else "1h=n/a (first run)"
    )
    print(
        f"[poll] {group.name}:  "
        f"{h_part}  "
        f"24h=${result['daily_cost']:.4f}"
        f"{'/' + f'${group.limits.daily:.2f}' if group.limits.daily else ''}  "
        f"[{status}]"
    )


def _print_top_keys(db: WatchdogDB) -> None:
    """Print the 5 most expensive API keys in the last 24 hours."""
    top = db.top_keys_by_cost(window_seconds=86400, limit=5)
    if not top:
        return
    print("[poll] Top 5 keys by cost (24h):")
    for i, (key_id, group_name, cost) in enumerate(top, 1):
        # Truncate key ID for display (show first 12 + last 4 chars).
        if len(key_id) > 20:
            display_key = key_id[:12] + "..." + key_id[-4:]
        else:
            display_key = key_id
        print(f"  {i}. {display_key:<25s}  ${cost:.4f}  ({group_name})")
