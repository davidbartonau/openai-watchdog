"""Periodic usage poller.

Designed to be called every N minutes (default 10).  Each invocation:

1. Determines the time window: ``last_poll_ts .. now``.
2. Fetches usage from the OpenAI API for that window.
3. Stores per-group dollar costs in SQLite.
4. Queries rolling 1-hour and 24-hour totals from the DB.
5. Evaluates soft and hard limits, triggers enforcement actions.
"""

import sys
import time
from typing import Optional

from openai_watchdog.config import WatchdogConfig, KeyGroup
from openai_watchdog.db import WatchdogDB
from openai_watchdog.monitor import aggregate_group_usage
from openai_watchdog.usage import UsageClient


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
    if last_poll is not None:
        # Query from last successful poll to now.
        query_start = last_poll
    else:
        # First run — look back one poll interval so we have *something*.
        query_start = now - cfg.poll.interval_seconds

    if verbose:
        print(f"[poll] window: {query_start} → {now}  "
              f"({(now - query_start) / 60:.1f} min)")

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

        db.store_usage(
            group_name=group.name,
            poll_ts=now,
            period_start=query_start,
            period_end=now,
            cost_usd=cost,
        )
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

        result = _evaluate_limits(group, hourly, daily)
        results.append(result)

        if verbose:
            _print_result(result, group)

    # Run enforcement (emails, key restriction).
    enforce_limits(results, cfg, db, client, verbose=verbose)

    # Prune old data (keep 7 days).
    db.prune_old_records()

    return results


def _evaluate_limits(
    group: KeyGroup,
    hourly_cost: float,
    daily_cost: float,
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

    if limits.hourly is not None:
        result["soft_hourly_exceeded"] = hourly_cost > limits.hourly
        result["hard_hourly_exceeded"] = hourly_cost > limits.hard_hourly

    if limits.daily is not None:
        result["soft_daily_exceeded"] = daily_cost > limits.daily
        result["hard_daily_exceeded"] = daily_cost > limits.hard_daily

    return result


def _print_result(result: dict, group: KeyGroup) -> None:
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
    print(
        f"[poll] {group.name}:  "
        f"1h=${result['hourly_cost']:.4f}"
        f"{'/' + f'${group.limits.hourly:.2f}' if group.limits.hourly else ''}  "
        f"24h=${result['daily_cost']:.4f}"
        f"{'/' + f'${group.limits.daily:.2f}' if group.limits.daily else ''}  "
        f"[{status}]"
    )
