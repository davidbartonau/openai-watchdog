"""Client for the OpenAI Organization Usage & Costs API.

These endpoints require an **Admin API key** (not a regular project key).
Set the OPENAI_ADMIN_KEY environment variable, or pass the key explicitly.

Reference: https://platform.openai.com/docs/api-reference/usage
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

BASE_URL = "https://api.openai.com/v1/organization"

# All supported usage bucket types and their endpoint paths.
USAGE_ENDPOINTS = {
    "completions":               "usage/completions",
    "embeddings":                "usage/embeddings",
    "images":                    "usage/images",
    "audio_speeches":            "usage/audio_speeches",
    "audio_transcriptions":      "usage/audio_transcriptions",
    "moderations":               "usage/moderations",
    "code_interpreter_sessions": "usage/code_interpreter_sessions",
    "vector_stores":             "usage/vector_stores",
}


@dataclass
class UsageBucket:
    """A single time-bucketed usage record."""
    start_time: int
    end_time: int
    results: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class UsageResponse:
    """Parsed response from a usage or costs endpoint."""
    object: str
    data: list[UsageBucket] = field(default_factory=list)
    next_page: Optional[str] = None


class UsageClient:
    """Thin client for the OpenAI Organization Usage & Costs API."""

    def __init__(self, admin_key: str, org_id: Optional[str] = None):
        self.admin_key = admin_key
        self.org_id = org_id

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{BASE_URL}/{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if qs:
                url = f"{url}?{qs}"

        headers = {"Authorization": f"Bearer {self.admin_key}"}
        if self.org_id:
            headers["OpenAI-Organization"] = self.org_id

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"OpenAI API error {exc.code} on GET {url}: {body}"
            ) from exc

    # ------------------------------------------------------------------
    # Usage endpoints
    # ------------------------------------------------------------------

    def get_usage(
        self,
        bucket_type: str,
        start_time: int,
        end_time: Optional[int] = None,
        bucket_width: str = "1d",
        group_by: Optional[list[str]] = None,
        api_key_ids: Optional[list[str]] = None,
        project_ids: Optional[list[str]] = None,
        models: Optional[list[str]] = None,
        limit: Optional[int] = None,
    ) -> UsageResponse:
        """Fetch usage for a specific bucket type.

        Args:
            bucket_type: One of the keys in USAGE_ENDPOINTS.
            start_time:  Unix timestamp (seconds).
            end_time:    Unix timestamp (seconds), or None for now.
            bucket_width: "1m", "1h", or "1d".
            group_by:    List of grouping dimensions, e.g. ["api_key_id", "model"].
            api_key_ids: Filter to specific API key IDs.
            project_ids: Filter to specific project IDs.
            models:      Filter to specific model names.
            limit:       Max buckets per page.
        """
        if bucket_type not in USAGE_ENDPOINTS:
            raise ValueError(
                f"Unknown bucket_type {bucket_type!r}. "
                f"Choose from: {', '.join(USAGE_ENDPOINTS)}"
            )

        params: dict[str, Any] = {
            "start_time": start_time,
            "bucket_width": bucket_width,
        }
        if end_time is not None:
            params["end_time"] = end_time
        if group_by:
            params["group_by"] = ",".join(group_by)
        if api_key_ids:
            params["api_key_ids"] = ",".join(api_key_ids)
        if project_ids:
            params["project_ids"] = ",".join(project_ids)
        if models:
            params["models"] = ",".join(models)
        if limit is not None:
            params["limit"] = limit

        return self._fetch_all_pages(USAGE_ENDPOINTS[bucket_type], params)

    def get_all_usage(
        self,
        start_time: int,
        end_time: Optional[int] = None,
        bucket_width: str = "1d",
        group_by: Optional[list[str]] = None,
    ) -> dict[str, UsageResponse]:
        """Fetch usage across all bucket types. Returns {bucket_type: UsageResponse}."""
        results = {}
        for btype in USAGE_ENDPOINTS:
            try:
                results[btype] = self.get_usage(
                    btype, start_time, end_time, bucket_width, group_by,
                )
            except RuntimeError:
                # Endpoint may not exist or key lacks access — skip silently.
                pass
        return results

    # ------------------------------------------------------------------
    # Costs endpoint
    # ------------------------------------------------------------------

    def get_costs(
        self,
        start_time: int,
        end_time: Optional[int] = None,
        group_by: Optional[list[str]] = None,
        project_ids: Optional[list[str]] = None,
        limit: Optional[int] = None,
    ) -> UsageResponse:
        """Fetch reconciled cost data (authoritative for billing).

        Note: costs only supports bucket_width=1d.
        """
        params: dict[str, Any] = {
            "start_time": start_time,
            "bucket_width": "1d",
        }
        if end_time is not None:
            params["end_time"] = end_time
        if group_by:
            params["group_by"] = ",".join(group_by)
        if project_ids:
            params["project_ids"] = ",".join(project_ids)
        if limit is not None:
            params["limit"] = limit

        return self._fetch_all_pages("costs", params)

    # ------------------------------------------------------------------
    # Pagination helper
    # ------------------------------------------------------------------

    def _fetch_all_pages(self, path: str, params: dict) -> UsageResponse:
        all_buckets: list[UsageBucket] = []
        page_cursor: Optional[str] = None

        while True:
            p = dict(params)
            if page_cursor:
                p["page"] = page_cursor

            raw = self._get(path, p)
            for item in raw.get("data", []):
                all_buckets.append(UsageBucket(
                    start_time=item["start_time"],
                    end_time=item["end_time"],
                    results=item.get("results", []),
                ))

            page_cursor = raw.get("next_page")
            if not page_cursor:
                break

        return UsageResponse(
            object=raw.get("object", "list"),
            data=all_buckets,
            next_page=None,
        )


# ------------------------------------------------------------------
# Convenience: quick timestamp helpers
# ------------------------------------------------------------------

def hours_ago(n: float) -> int:
    return int(time.time() - n * 3600)


def days_ago(n: float) -> int:
    return int(time.time() - n * 86400)
