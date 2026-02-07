# OpenAI Watchdog — Progress & Plan

## What It Is

A Python CLI tool for programmers/sysadmins to monitor OpenAI API usage and costs.
Supports grouping API keys, setting rate limits ($/h), downloading reports,
and tracking spend across models.

## What's Done

### 1. Project scaffold (committed)
- `uv` project with `pyproject.toml`, `requirements.txt`
- Dependencies: `openai>=1.0.0`, `pyyaml>=6.0`
- Package: `openai_watchdog/` with `__init__.py`
- Virtual env at `.venv/` (Python 3.11)
- `trial.py` — verified API connectivity with a chat completion call

### 2. Pricing module — `openai_watchdog/pricing.py` (committed)
- Hardcoded USD-per-1M-token rates for all current models (as of 2026-02):
  - **Text/chat**: GPT-5.x, GPT-4.1, GPT-4o, o-series, codex, search, realtime
  - **Image tokens**: gpt-image-1/1.5, chatgpt-image-latest
  - **Audio tokens**: realtime, gpt-audio
  - **Embeddings**: text-embedding-3-small/large, ada-002
  - **Video**: sora-2/2-pro (per second)
  - **Tools**: code interpreter, file search, web search
- Helper functions: `estimate_text_cost()`, `estimate_embedding_cost()`
- Prefix-matching for dated model variants (e.g. `gpt-4o-mini-2024-07-18` → `gpt-4o-mini`)

### 3. Usage API client — `openai_watchdog/usage.py` (committed)
- HTTP client for OpenAI Organization Usage & Costs API
- Endpoints covered:
  - `/v1/organization/usage/completions`
  - `/v1/organization/usage/embeddings`
  - `/v1/organization/usage/images`
  - `/v1/organization/usage/audio_speeches`
  - `/v1/organization/usage/audio_transcriptions`
  - `/v1/organization/usage/moderations`
  - `/v1/organization/usage/code_interpreter_sessions`
  - `/v1/organization/usage/vector_stores`
  - `/v1/organization/costs`
- Supports: time ranges, bucket widths (1m/1h/1d), grouping by
  api_key_id/model/project_id/user_id, filtering, auto-pagination
- **Requires an Admin API key** (`OPENAI_ADMIN_KEY` env var)

### 4. CLI — `openai_watchdog/cli.py` (committed)
- Three sub-commands:
  - `usage` — fetch usage across all endpoint types, grouped by api_key/model
  - `costs` — fetch reconciled billing data (authoritative for invoices)
  - `prices` — display the built-in pricing table
- Flexible time args: `24h`, `7d`, unix timestamps, ISO dates
- Run with: `uv run python -m openai_watchdog.cli <command>`

### 5. YAML configuration — `openai_watchdog/config.py`
- Loads `watchdog.yaml` / `watchdog.yml` (auto-discovered or explicit `--config`)
- Schema supports:
  - `admin_key_env` — env var name for the admin key
  - `key_groups` — named groups with `api_key_ids` list and optional `max_dollars_per_hour`
  - `alerts` — `stdout` (bool) and `webhook_url` (optional)
  - `export` — `format` (csv/json) and `output_dir`
- Validation: rejects unknown formats, missing files, malformed groups
- Example config: `watchdog.example.yaml`

### 6. Key group reporting — `openai_watchdog/monitor.py`
- `aggregate_group_usage()` — fetches completions + embeddings for a key group's
  API keys and aggregates tokens, requests, and estimated costs
- Per-model cost breakdown within each group
- CLI: `openai-watchdog groups [--since 24h] [--export]`

### 7. Rate limit monitoring — `openai_watchdog/monitor.py`
- `check_rate_limits()` — checks last-hour spend for every group with a
  `max_dollars_per_hour` threshold
- Reports actual $/h, limit, percentage, and exceeded status
- CLI: `openai-watchdog watch [--export]`
- Exits non-zero on breach (useful for cron/CI pipelines)

### 8. Alerts — `openai_watchdog/monitor.py`
- `send_alerts()` — fires when rate limits are exceeded
- **stdout**: formatted warning to stderr
- **Webhook**: POST JSON payload to configured URL (e.g. Slack incoming webhook)
- Payload includes group name, actual spend, limit, and percentage

### 9. CSV/JSON export — `openai_watchdog/export.py`
- `export_group_summaries()` — group usage to CSV or JSON
- `export_rate_limit_checks()` — rate limit results to CSV or JSON
- `export_raw_usage()` — raw API usage data (all bucket types) to CSV or JSON
- Auto-timestamped filenames in configurable output directory
- CLI: `openai-watchdog export [--format csv|json] [--output-dir ./reports]`

## Key Finding: Admin Key Required

The `/organization/usage/*` and `/organization/costs` endpoints return **403**
with a regular project API key, even with all scopes enabled. They require an
**Admin API key** created at:

  https://platform.openai.com/settings/organization/admin-keys

Set it as `OPENAI_ADMIN_KEY` in the environment.

## CLI Commands Summary

| Command  | Description                                      |
|----------|--------------------------------------------------|
| `usage`  | Show raw usage across all API endpoint types      |
| `costs`  | Show reconciled billing data                      |
| `prices` | Display built-in model pricing table              |
| `groups` | Show usage aggregated by configured key groups    |
| `watch`  | Check rate limits and alert on breaches           |
| `export` | Export raw usage data to CSV or JSON              |

All commands accept `--config <path>` to specify a config file.

## What's Next

### Immediate (need admin key to test)
1. **Test all commands** end-to-end with the admin key
2. **Verify cost calculation accuracy** — compare estimated costs from our
   pricing tables against the reconciled costs from the `/costs` endpoint

### Nice-to-haves
3. **Auto-refresh pricing** — fetch current rates from OpenAI (if they expose
   a pricing API) instead of hardcoding
4. **Historical trend display** — simple ASCII chart of spend over time
5. **Daemon mode** — long-running process with configurable poll interval
