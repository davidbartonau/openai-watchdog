"""CLI entry point for openai-watchdog."""

import argparse
import os
import signal
import sys
import time
from datetime import datetime, timezone

from openai_watchdog.config import load_config
from openai_watchdog.pricing import (
    TEXT_PRICING,
    EMBEDDING_PRICING,
    estimate_text_cost,
    estimate_embedding_cost,
)
from openai_watchdog.usage import UsageClient, USAGE_ENDPOINTS, days_ago, hours_ago


def _get_admin_key(config_path=None) -> str:
    """Resolve admin key: config -> OPENAI_ADMIN_KEY -> OPENAI_API_KEY."""
    cfg = load_config(config_path)
    key = cfg.get_admin_key()
    if not key:
        key = os.environ.get("OPENAI_ADMIN_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        print(
            "Error: Set OPENAI_ADMIN_KEY (preferred) or OPENAI_API_KEY.",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def _ts_label(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ------------------------------------------------------------------
# Sub-commands
# ------------------------------------------------------------------

def cmd_usage(args: argparse.Namespace) -> None:
    """Fetch and display usage across all bucket types."""
    client = UsageClient(_get_admin_key(getattr(args, "config", None)))

    start = _parse_time_arg(args.since)
    end = _parse_time_arg(args.until) if args.until else None
    group_by = args.group_by.split(",") if args.group_by else ["api_key_id", "model"]

    bucket_types = args.types.split(",") if args.types else list(USAGE_ENDPOINTS)

    print(f"Fetching usage from {_ts_label(start)}"
          f"{' to ' + _ts_label(end) if end else ' to now'}...")
    print(f"Grouping by: {', '.join(group_by)}")
    print(f"Bucket width: {args.bucket_width}")
    print()

    for btype in bucket_types:
        if btype not in USAGE_ENDPOINTS:
            print(f"  [skip] Unknown type: {btype}")
            continue
        try:
            resp = client.get_usage(
                btype, start, end,
                bucket_width=args.bucket_width,
                group_by=group_by,
            )
        except RuntimeError as exc:
            print(f"  [{btype}] ERROR: {exc}")
            continue

        total_results = sum(len(b.results) for b in resp.data)
        if total_results == 0:
            continue

        print(f"── {btype} ({'─' * (60 - len(btype))})")
        for bucket in resp.data:
            if not bucket.results:
                continue
            print(f"  {_ts_label(bucket.start_time)} → {_ts_label(bucket.end_time)}")
            for r in bucket.results:
                _print_result_row(btype, r, group_by)
        print()


def cmd_costs(args: argparse.Namespace) -> None:
    """Fetch reconciled cost data."""
    client = UsageClient(_get_admin_key(getattr(args, "config", None)))

    start = _parse_time_arg(args.since)
    end = _parse_time_arg(args.until) if args.until else None
    group_by = args.group_by.split(",") if args.group_by else ["line_item"]

    print(f"Fetching costs from {_ts_label(start)}"
          f"{' to ' + _ts_label(end) if end else ' to now'}...")
    print()

    resp = client.get_costs(start, end, group_by=group_by)

    grand_total = 0.0
    for bucket in resp.data:
        if not bucket.results:
            continue
        print(f"  {_ts_label(bucket.start_time)} → {_ts_label(bucket.end_time)}")
        for r in bucket.results:
            amount = r.get("amount", {})
            value = amount.get("value", 0) or 0
            currency = amount.get("currency", "usd")
            dims = {k: v for k, v in r.items() if k not in ("amount", "object")}
            dim_str = "  ".join(f"{k}={v}" for k, v in dims.items()) if dims else ""
            # Costs API returns cents — convert to dollars
            dollars = value / 100.0
            grand_total += dollars
            print(f"    ${dollars:>10.4f}  {dim_str}")

    print(f"\n  Total: ${grand_total:.4f}")


def cmd_prices(args: argparse.Namespace) -> None:
    """Display the built-in pricing table."""
    print("Text / Chat Completion Models  (USD per 1M tokens)")
    print(f"  {'Model':<40s} {'Input':>8s} {'Cached':>8s} {'Output':>8s}")
    print(f"  {'─'*40} {'─'*8} {'─'*8} {'─'*8}")
    for model, p in sorted(TEXT_PRICING.items()):
        cached = f"${p.cached_input:.3f}" if p.cached_input is not None else "-"
        print(f"  {model:<40s} ${p.input:>7.3f} {cached:>8s} ${p.output:>7.3f}")

    print()
    print("Embedding Models  (USD per 1M tokens)")
    print(f"  {'Model':<40s} {'Input':>8s}")
    print(f"  {'─'*40} {'─'*8}")
    for model, rate in sorted(EMBEDDING_PRICING.items()):
        print(f"  {model:<40s} ${rate:>7.3f}")


def cmd_groups(args: argparse.Namespace) -> None:
    """Show usage aggregated by configured key groups."""
    from openai_watchdog.monitor import aggregate_group_usage
    from openai_watchdog.export import export_group_summaries

    cfg = load_config(getattr(args, "config", None))
    if not cfg.key_groups:
        print("No key groups configured. Add key_groups to watchdog.yaml.")
        return

    client = UsageClient(_get_admin_key(getattr(args, "config", None)))
    start = _parse_time_arg(args.since)
    end = _parse_time_arg(args.until) if args.until else None

    print(f"Fetching group usage from {_ts_label(start)}"
          f"{' to ' + _ts_label(end) if end else ' to now'}...")
    print()

    summaries = []
    for name, group in cfg.key_groups.items():
        summary = aggregate_group_usage(client, group, start, end)
        summaries.append(summary)

        print(f"── {name} ({'─' * (60 - len(name))})")
        print(f"  API keys: {len(group.api_key_ids)}")
        print(f"  Requests: {summary.total_requests:,}")
        print(f"  Input tokens:  {summary.total_input_tokens:,}"
              f"  (cached: {summary.total_cached_tokens:,})")
        print(f"  Output tokens: {summary.total_output_tokens:,}")
        print(f"  Estimated cost: ${summary.estimated_cost_usd:.4f}")
        if summary.model_costs:
            print("  By model:")
            for model, cost in sorted(summary.model_costs.items(),
                                      key=lambda x: x[1], reverse=True):
                print(f"    {model:<40s} ${cost:.4f}")
        limits = group.limits
        if limits.has_any_limit:
            parts = []
            if limits.hourly is not None:
                parts.append(f"${limits.hourly:.2f}/h (hard ${limits.hard_hourly:.2f})")
            if limits.daily is not None:
                parts.append(f"${limits.daily:.2f}/day (hard ${limits.hard_daily:.2f})")
            print(f"  Limits: {', '.join(parts)}")
        print()

    if args.export:
        fmt = args.export_format or cfg.export.format
        out_dir = args.export_dir or cfg.export.output_dir
        path = export_group_summaries(summaries, fmt=fmt, output_dir=out_dir)
        print(f"Exported to {path}")


def cmd_watch(args: argparse.Namespace) -> None:
    """One-shot rate-limit check (legacy).  Use `poll` for continuous monitoring."""
    from openai_watchdog.monitor import check_rate_limits, send_alerts
    from openai_watchdog.export import export_rate_limit_checks

    cfg = load_config(getattr(args, "config", None))
    if not cfg.key_groups:
        print("No key groups configured. Add key_groups to watchdog.yaml.")
        return

    groups_with_limits = [g for g in cfg.key_groups.values()
                          if g.limits.has_any_limit]
    if not groups_with_limits:
        print("No rate limits configured. Set limits on key groups.")
        return

    client = UsageClient(_get_admin_key(getattr(args, "config", None)))

    print(f"Checking rate limits for {len(groups_with_limits)} group(s)...")
    checks = check_rate_limits(client, cfg)

    for c in checks:
        status = "EXCEEDED" if c.exceeded else "OK"
        print(f"  {c.group_name:<30s} ${c.actual_dollars_last_hour:.4f}/h"
              f"  limit=${c.max_dollars_per_hour:.2f}/h"
              f"  ({c.pct_of_limit:.0f}%)  [{status}]")

    breaches = [c for c in checks if c.exceeded]
    if breaches:
        print()
        send_alerts(checks, cfg.alerts)

    if args.export:
        fmt = args.export_format or cfg.export.format
        out_dir = args.export_dir or cfg.export.output_dir
        path = export_rate_limit_checks(checks, fmt=fmt, output_dir=out_dir)
        print(f"Exported to {path}")

    if breaches:
        sys.exit(1)


def cmd_poll(args: argparse.Namespace) -> None:
    """Run the usage poller (one-shot or continuous loop).

    One-shot:   openai-watchdog poll --once
    Continuous: openai-watchdog poll           (runs every interval_seconds)
    """
    from openai_watchdog.db import WatchdogDB
    from openai_watchdog.poller import run_poll_cycle

    cfg = load_config(getattr(args, "config", None))
    if not cfg.key_groups:
        print("No key groups configured. Add key_groups to watchdog.yaml.")
        return

    client = UsageClient(_get_admin_key(getattr(args, "config", None)))
    db = WatchdogDB(args.db or cfg.poll.db_path)
    db.connect()
    interval = args.interval or cfg.poll.interval_seconds

    if args.once:
        print(f"[poll] Running single poll cycle...")
        results = run_poll_cycle(cfg, client, db, verbose=True)
        _print_poll_summary(results)
        db.close()
        # Exit non-zero if any hard limits exceeded.
        if any(r.get("hard_hourly_exceeded") or r.get("hard_daily_exceeded")
               for r in results):
            sys.exit(2)
        if any(r.get("soft_hourly_exceeded") or r.get("soft_daily_exceeded")
               for r in results):
            sys.exit(1)
        return

    # Continuous loop.
    print(f"[poll] Starting continuous polling every {interval}s "
          f"({interval / 60:.0f} min).  Ctrl-C to stop.")

    # Handle graceful shutdown.
    running = True

    def _shutdown(signum, frame):
        nonlocal running
        print("\n[poll] Shutting down...")
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while running:
        try:
            results = run_poll_cycle(cfg, client, db, verbose=True)
            _print_poll_summary(results)
        except Exception as exc:
            print(f"[poll] ERROR in poll cycle: {exc}", file=sys.stderr)

        # Sleep in small increments so we can respond to signals.
        deadline = time.time() + interval
        while running and time.time() < deadline:
            time.sleep(min(1.0, deadline - time.time()))

    db.close()
    print("[poll] Stopped.")


def cmd_status(args: argparse.Namespace) -> None:
    """Show current rolling costs from the database."""
    from openai_watchdog.db import WatchdogDB

    cfg = load_config(getattr(args, "config", None))
    db = WatchdogDB(args.db or cfg.poll.db_path)
    db.connect()

    last_poll = db.last_successful_poll_ts()
    if last_poll is None:
        print("No poll data yet. Run `openai-watchdog poll --once` first.")
        db.close()
        return

    print(f"Last poll: {_ts_label(last_poll)}")
    print()

    any_breach = False
    for name, group in cfg.key_groups.items():
        hourly = db.rolling_cost_hourly(name)
        daily = db.rolling_cost_daily(name)

        h_limit = group.limits.hourly
        d_limit = group.limits.daily
        h_hard = group.limits.hard_hourly
        d_hard = group.limits.hard_daily

        flags = []
        if h_limit and hourly > h_limit:
            flags.append("SOFT-HOURLY")
        if d_limit and daily > d_limit:
            flags.append("SOFT-DAILY")
        if h_hard and hourly > h_hard:
            flags.append("HARD-HOURLY")
        if d_hard and daily > d_hard:
            flags.append("HARD-DAILY")
        if flags:
            any_breach = True

        status = ", ".join(flags) if flags else "OK"

        h_str = f"${hourly:.4f}"
        if h_limit:
            h_str += f" / ${h_limit:.2f}"
        d_str = f"${daily:.4f}"
        if d_limit:
            d_str += f" / ${d_limit:.2f}"

        print(f"  {name:<25s}  1h: {h_str:<20s}  24h: {d_str:<20s}  [{status}]")

    db.close()
    if any_breach:
        sys.exit(1)


def cmd_export(args: argparse.Namespace) -> None:
    """Export raw usage data to CSV or JSON."""
    from openai_watchdog.export import export_raw_usage

    cfg = load_config(getattr(args, "config", None))
    client = UsageClient(_get_admin_key(getattr(args, "config", None)))

    start = _parse_time_arg(args.since)
    end = _parse_time_arg(args.until) if args.until else None
    bucket_types = args.types.split(",") if args.types else list(USAGE_ENDPOINTS)
    fmt = args.format or cfg.export.format
    out_dir = args.output_dir or cfg.export.output_dir

    print(f"Fetching usage from {_ts_label(start)}"
          f"{' to ' + _ts_label(end) if end else ' to now'}...")

    usage_by_type = {}
    for btype in bucket_types:
        if btype not in USAGE_ENDPOINTS:
            continue
        try:
            usage_by_type[btype] = client.get_usage(
                btype, start, end, bucket_width="1h",
            )
        except RuntimeError as exc:
            print(f"  [{btype}] ERROR: {exc}", file=sys.stderr)

    path = export_raw_usage(usage_by_type, fmt=fmt, output_dir=out_dir)
    print(f"Exported to {path}")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_time_arg(value: str) -> int:
    """Parse a time argument: 'Nh' for hours ago, 'Nd' for days ago, or unix timestamp."""
    value = value.strip()
    if value.endswith("h"):
        return hours_ago(float(value[:-1]))
    if value.endswith("d"):
        return days_ago(float(value[:-1]))
    try:
        return int(value)
    except ValueError:
        # Try ISO date
        dt = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        return int(dt.timestamp())


def _print_result_row(btype: str, r: dict, group_by: list[str]) -> None:
    """Pretty-print one result row from a usage bucket."""
    dims = []
    for g in group_by:
        val = r.get(g, "")
        if val:
            dims.append(f"{g}={val}")
    dim_str = "  ".join(dims) if dims else ""

    # Extract key metrics depending on bucket type
    parts = []
    for key in ("input_tokens", "output_tokens", "input_cached_tokens",
                "num_model_requests", "num_images", "characters", "seconds",
                "num_sessions", "input_audio_tokens", "output_audio_tokens"):
        val = r.get(key, 0)
        if val:
            label = key.replace("_", " ")
            parts.append(f"{label}={val:,}")

    # Estimate cost if we have token info
    cost_str = ""
    if btype == "completions":
        model = r.get("model", "")
        inp = r.get("input_tokens", 0) or 0
        out = r.get("output_tokens", 0) or 0
        cached = r.get("input_cached_tokens", 0) or 0
        cost = estimate_text_cost(model, inp, out, cached)
        if cost is not None:
            cost_str = f"  ~${cost:.4f}"
    elif btype == "embeddings":
        model = r.get("model", "")
        inp = r.get("input_tokens", 0) or 0
        cost = estimate_embedding_cost(model, inp)
        if cost is not None:
            cost_str = f"  ~${cost:.6f}"

    metrics_str = "  ".join(parts)
    print(f"    {dim_str:<50s} {metrics_str}{cost_str}")


def _print_poll_summary(results: list[dict]) -> None:
    """Print a compact summary line after a poll cycle."""
    soft = sum(1 for r in results
               if r.get("soft_hourly_exceeded") or r.get("soft_daily_exceeded"))
    hard = sum(1 for r in results
               if r.get("hard_hourly_exceeded") or r.get("hard_daily_exceeded"))
    if hard:
        print(f"[poll] {hard} group(s) at HARD limit, {soft} at soft limit")
    elif soft:
        print(f"[poll] {soft} group(s) at soft limit")
    else:
        print(f"[poll] All {len(results)} group(s) within limits")


# ------------------------------------------------------------------
# Argument parser
# ------------------------------------------------------------------

def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add --config flag shared by all subcommands."""
    parser.add_argument(
        "--config", default=None,
        help="Path to watchdog.yaml config file",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openai-watchdog",
        description="Monitor and report on OpenAI API usage and costs.",
    )
    sub = parser.add_subparsers(dest="command")

    # --- usage ---
    p_usage = sub.add_parser("usage", help="Show usage across API endpoints")
    _add_common_args(p_usage)
    p_usage.add_argument(
        "--since", default="24h",
        help="Start time: e.g. '24h', '7d', unix timestamp, or ISO date (default: 24h)",
    )
    p_usage.add_argument("--until", default=None, help="End time (same formats)")
    p_usage.add_argument(
        "--group-by", default=None,
        help="Comma-separated grouping dims (default: api_key_id,model)",
    )
    p_usage.add_argument(
        "--types", default=None,
        help=f"Comma-separated bucket types (default: all). Options: {','.join(USAGE_ENDPOINTS)}",
    )
    p_usage.add_argument(
        "--bucket-width", default="1d", choices=["1m", "1h", "1d"],
        help="Time bucket granularity (default: 1d)",
    )

    # --- costs ---
    p_costs = sub.add_parser("costs", help="Show reconciled cost data")
    _add_common_args(p_costs)
    p_costs.add_argument("--since", default="7d", help="Start time (default: 7d)")
    p_costs.add_argument("--until", default=None, help="End time")
    p_costs.add_argument(
        "--group-by", default=None,
        help="Comma-separated grouping dims (default: line_item)",
    )

    # --- prices ---
    p_prices = sub.add_parser("prices", help="Display built-in pricing table")
    _add_common_args(p_prices)

    # --- groups ---
    p_groups = sub.add_parser("groups", help="Show usage by configured key groups")
    _add_common_args(p_groups)
    p_groups.add_argument("--since", default="24h", help="Start time (default: 24h)")
    p_groups.add_argument("--until", default=None, help="End time")
    p_groups.add_argument(
        "--export", action="store_true",
        help="Export results to file",
    )
    p_groups.add_argument("--export-format", choices=["csv", "json"], default=None)
    p_groups.add_argument("--export-dir", default=None)

    # --- watch (legacy one-shot) ---
    p_watch = sub.add_parser(
        "watch",
        help="One-shot rate-limit check (use `poll` for continuous)",
    )
    _add_common_args(p_watch)
    p_watch.add_argument(
        "--export", action="store_true",
        help="Export check results to file",
    )
    p_watch.add_argument("--export-format", choices=["csv", "json"], default=None)
    p_watch.add_argument("--export-dir", default=None)

    # --- poll (new: periodic monitoring with DB) ---
    p_poll = sub.add_parser(
        "poll",
        help="Periodic usage polling with rolling limits and enforcement",
    )
    _add_common_args(p_poll)
    p_poll.add_argument(
        "--once", action="store_true",
        help="Run a single poll cycle and exit",
    )
    p_poll.add_argument(
        "--interval", type=int, default=None,
        help="Poll interval in seconds (default: from config or 600)",
    )
    p_poll.add_argument(
        "--db", default=None,
        help="Path to SQLite database (default: from config or watchdog.db)",
    )

    # --- status (query rolling costs from DB) ---
    p_status = sub.add_parser(
        "status",
        help="Show current rolling costs from the poll database",
    )
    _add_common_args(p_status)
    p_status.add_argument(
        "--db", default=None,
        help="Path to SQLite database (default: from config or watchdog.db)",
    )

    # --- export ---
    p_export = sub.add_parser("export", help="Export raw usage data to CSV/JSON")
    _add_common_args(p_export)
    p_export.add_argument("--since", default="24h", help="Start time (default: 24h)")
    p_export.add_argument("--until", default=None, help="End time")
    p_export.add_argument(
        "--types", default=None,
        help=f"Comma-separated bucket types (default: all). Options: {','.join(USAGE_ENDPOINTS)}",
    )
    p_export.add_argument(
        "--format", choices=["csv", "json"], default=None,
        help="Output format (default: from config or csv)",
    )
    p_export.add_argument(
        "--output-dir", default=None,
        help="Output directory (default: from config or ./reports)",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    commands = {
        "usage": cmd_usage,
        "costs": cmd_costs,
        "prices": cmd_prices,
        "groups": cmd_groups,
        "watch": cmd_watch,
        "poll": cmd_poll,
        "status": cmd_status,
        "export": cmd_export,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
