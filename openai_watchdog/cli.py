"""CLI entry point for openai-watchdog."""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from openai_watchdog.pricing import (
    TEXT_PRICING,
    EMBEDDING_PRICING,
    estimate_text_cost,
    estimate_embedding_cost,
)
from openai_watchdog.usage import UsageClient, USAGE_ENDPOINTS, days_ago, hours_ago


def _get_admin_key() -> str:
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
    client = UsageClient(_get_admin_key())

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
    client = UsageClient(_get_admin_key())

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


# ------------------------------------------------------------------
# Argument parser
# ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openai-watchdog",
        description="Monitor and report on OpenAI API usage and costs.",
    )
    sub = parser.add_subparsers(dest="command")

    # --- usage ---
    p_usage = sub.add_parser("usage", help="Show usage across API endpoints")
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
    p_costs.add_argument("--since", default="7d", help="Start time (default: 7d)")
    p_costs.add_argument("--until", default=None, help="End time")
    p_costs.add_argument(
        "--group-by", default=None,
        help="Comma-separated grouping dims (default: line_item)",
    )

    # --- prices ---
    sub.add_parser("prices", help="Display built-in pricing table")

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
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
