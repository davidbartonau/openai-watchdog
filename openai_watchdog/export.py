"""CSV and JSON export for usage and cost reports."""

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai_watchdog.monitor import GroupUsageSummary, RateLimitCheck
from openai_watchdog.usage import UsageResponse


def export_group_summaries(
    summaries: list[GroupUsageSummary],
    fmt: str = "csv",
    output_dir: str = "./reports",
) -> str:
    """Export group usage summaries to a file.  Returns the output path."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")

    if fmt == "json":
        path = Path(output_dir) / f"group_usage_{ts}.json"
        data = [_summary_to_dict(s) for s in summaries]
        path.write_text(json.dumps(data, indent=2))
    else:
        path = Path(output_dir) / f"group_usage_{ts}.csv"
        path.write_text(_summaries_to_csv(summaries))

    return str(path)


def export_rate_limit_checks(
    checks: list[RateLimitCheck],
    fmt: str = "csv",
    output_dir: str = "./reports",
) -> str:
    """Export rate-limit check results to a file.  Returns the output path."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")

    if fmt == "json":
        path = Path(output_dir) / f"rate_limits_{ts}.json"
        data = [
            {
                "group": c.group_name,
                "max_dollars_per_hour": c.max_dollars_per_hour,
                "actual_dollars_last_hour": round(c.actual_dollars_last_hour, 6),
                "exceeded": c.exceeded,
                "pct_of_limit": round(c.pct_of_limit, 1),
            }
            for c in checks
        ]
        path.write_text(json.dumps(data, indent=2))
    else:
        path = Path(output_dir) / f"rate_limits_{ts}.csv"
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "group", "max_dollars_per_hour", "actual_dollars_last_hour",
            "exceeded", "pct_of_limit",
        ])
        for c in checks:
            writer.writerow([
                c.group_name, c.max_dollars_per_hour,
                round(c.actual_dollars_last_hour, 6),
                c.exceeded, round(c.pct_of_limit, 1),
            ])
        path.write_text(buf.getvalue())

    return str(path)


def export_raw_usage(
    usage_by_type: dict[str, UsageResponse],
    fmt: str = "csv",
    output_dir: str = "./reports",
) -> str:
    """Export raw usage data (all bucket types) to a file."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")

    if fmt == "json":
        path = Path(output_dir) / f"raw_usage_{ts}.json"
        data: dict[str, Any] = {}
        for btype, resp in usage_by_type.items():
            data[btype] = [
                {
                    "start_time": b.start_time,
                    "end_time": b.end_time,
                    "results": b.results,
                }
                for b in resp.data
            ]
        path.write_text(json.dumps(data, indent=2))
    else:
        path = Path(output_dir) / f"raw_usage_{ts}.csv"
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "bucket_type", "start_time", "end_time", "result_json",
        ])
        for btype, resp in usage_by_type.items():
            for b in resp.data:
                for r in b.results:
                    writer.writerow([
                        btype, b.start_time, b.end_time, json.dumps(r),
                    ])
        path.write_text(buf.getvalue())

    return str(path)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _summary_to_dict(s: GroupUsageSummary) -> dict[str, Any]:
    return {
        "group_name": s.group_name,
        "api_key_ids": s.api_key_ids,
        "total_input_tokens": s.total_input_tokens,
        "total_output_tokens": s.total_output_tokens,
        "total_cached_tokens": s.total_cached_tokens,
        "total_requests": s.total_requests,
        "estimated_cost_usd": round(s.estimated_cost_usd, 6),
        "model_costs": {k: round(v, 6) for k, v in s.model_costs.items()},
    }


def _summaries_to_csv(summaries: list[GroupUsageSummary]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "group_name", "total_input_tokens", "total_output_tokens",
        "total_cached_tokens", "total_requests", "estimated_cost_usd",
    ])
    for s in summaries:
        writer.writerow([
            s.group_name, s.total_input_tokens, s.total_output_tokens,
            s.total_cached_tokens, s.total_requests,
            round(s.estimated_cost_usd, 6),
        ])
    return buf.getvalue()
