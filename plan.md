# OpenAI Watchdog — Progress & Plan

## What It Is

A Python CLI tool for programmers/sysadmins to monitor OpenAI API usage and costs.
Supports grouping API keys, setting rolling hourly/daily spend limits with
soft (email warning) and hard (key restriction) enforcement, periodic polling
with SQLite-backed history, and CSV/JSON reporting.

## Architecture

```
watchdog.yaml          ← config: key groups, limits, alerts, SMTP
    │
    ▼
┌──────────┐   every N min   ┌──────────┐       ┌──────────────┐
│  poller  │ ───────────────→│ OpenAI   │       │  SQLite DB   │
│          │ fetch usage     │ Usage API│       │ (watchdog.db)│
│          │ ←───────────────│          │       │              │
│          │ store $ cost    └──────────┘       │ usage_records│
│          │ ──────────────────────────────────→│ poll_runs    │
│          │                                    │ alerts_sent  │
│          │ query rolling 1h / 24h totals      │              │
│          │ ←──────────────────────────────────│              │
└──────────┘                                    └──────────────┘
    │
    │ evaluate soft / hard limits
    ▼
┌──────────────┐
│ enforcement  │
│              │──→ soft: email owner + stdout/webhook
│              │──→ hard: restrict key via Admin API + email
└──────────────┘
```

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
- Endpoints covered: completions, embeddings, images, audio_speeches,
  audio_transcriptions, moderations, code_interpreter_sessions, vector_stores, costs
- Supports: time ranges, bucket widths (1m/1h/1d), grouping, filtering, auto-pagination
- **Requires an Admin API key** (`OPENAI_ADMIN_KEY` env var)

### 4. YAML configuration — `openai_watchdog/config.py`
- Loads `watchdog.yaml` / `watchdog.yml` (auto-discovered or explicit `--config`)
- Schema:
  - `admin_key_env` — env var name for the admin key
  - `poll.interval_seconds` — polling frequency (default 600 = 10 min)
  - `poll.db_path` — SQLite database location
  - `key_groups` — named groups with:
    - `api_key_ids` — list of API key IDs in the group
    - `owner_email` — email address for soft-limit notifications
    - `limits.hourly` — soft hourly spend limit in USD
    - `limits.daily` — soft daily spend limit in USD
    - `limits.hard_multiplier` — hard = soft × this (default 1.5, i.e. 150%)
  - `alerts.stdout`, `alerts.webhook_url`, `alerts.email.*` (SMTP config)
  - `export.format`, `export.output_dir`
- Backward-compatible with old `max_dollars_per_hour` field
- Example config: `watchdog.example.yaml`

### 5. SQLite database — `openai_watchdog/db.py`
- Tables: `poll_runs`, `usage_records`, `alerts_sent`
- `store_usage()` — record per-group dollar cost from each poll interval
- `rolling_cost_hourly()` / `rolling_cost_daily()` — sum costs over rolling
  1-hour / 24-hour windows from stored data
- `was_alert_sent_recently()` — dedup alerts with configurable cooldown
- `prune_old_records()` — auto-cleanup of data older than 7 days
- WAL mode for safe concurrent reads

### 6. Periodic poller — `openai_watchdog/poller.py`
- `run_poll_cycle()` — single poll iteration:
  1. Determine window: `last_successful_poll_ts` → now
  2. Fetch usage for each key group via Usage API (only the delta)
  3. Store per-group cost in SQLite
  4. Query rolling 1h + 24h totals from DB
  5. Evaluate soft/hard limits for hourly + daily
  6. Trigger enforcement actions
  7. Prune old records
- Designed to be called every 10 minutes (or any configured interval)

### 7. Enforcement — `openai_watchdog/enforcement.py`
- **Soft limit** (hourly or daily exceeded):
  - Print warning to stderr
  - Email the key group's `owner_email` via SMTP
  - POST to webhook URL
  - 1-hour cooldown to avoid spamming
- **Hard limit** (150% of soft, hourly or daily):
  - All of the above, plus:
  - Restrict each API key in the group via OpenAI Admin API
    (`POST /v1/organization/api_keys/{key_id}`) — renames key with
    `[RESTRICTED by watchdog]` prefix
  - `restore_api_key()` available for manual recovery

### 8. Key group reporting — `openai_watchdog/monitor.py`
- `aggregate_group_usage()` — fetch completions + embeddings per group,
  aggregate tokens/requests/estimated costs with per-model breakdown
- `check_rate_limits()` — legacy one-shot hourly check

### 9. CSV/JSON export — `openai_watchdog/export.py`
- `export_group_summaries()` — group usage to CSV or JSON
- `export_rate_limit_checks()` — rate limit results to CSV or JSON
- `export_raw_usage()` — raw API usage data to CSV or JSON
- Auto-timestamped filenames in configurable output directory

## CLI Commands

| Command  | Description                                               |
|----------|-----------------------------------------------------------|
| `usage`  | Show raw usage across all API endpoint types               |
| `costs`  | Show reconciled billing data                               |
| `prices` | Display built-in model pricing table                       |
| `groups` | Show usage aggregated by configured key groups             |
| `watch`  | One-shot rate-limit check (legacy)                         |
| `poll`   | **Periodic polling** with rolling limits and enforcement   |
| `status` | Show current rolling costs from the poll database          |
| `export` | Export raw usage data to CSV or JSON                       |

### Key workflows

```bash
# One-shot poll (e.g. from cron)
openai-watchdog poll --once

# Continuous daemon (runs every 10 min by default)
openai-watchdog poll

# Check current status without polling
openai-watchdog status

# Override poll interval
openai-watchdog poll --interval 300  # every 5 minutes
```

All commands accept `--config <path>` to specify a config file.

## Limit Logic

For each key group, limits are evaluated against rolling windows stored in SQLite:

| Check          | Window   | Threshold             | Action                        |
|----------------|----------|-----------------------|-------------------------------|
| Soft hourly    | Last 1h  | `limits.hourly`       | Email owner + alert           |
| Soft daily     | Last 24h | `limits.daily`        | Email owner + alert           |
| Hard hourly    | Last 1h  | `hourly × 1.5`       | Restrict keys + email + alert |
| Hard daily     | Last 24h | `daily × 1.5`        | Restrict keys + email + alert |

Example: `hourly: 20.0, daily: 30.0, hard_multiplier: 1.5`
→ soft limits: $20/h, $30/day
→ hard limits: $30/h, $45/day

## Key Finding: Admin Key Required

The `/organization/usage/*` and `/organization/costs` endpoints return **403**
with a regular project API key, even with all scopes enabled. They require an
**Admin API key** created at:

  https://platform.openai.com/settings/organization/admin-keys

Set it as `OPENAI_ADMIN_KEY` in the environment.

## What's Next

### Immediate (need admin key to test)
1. **Test all commands** end-to-end with the admin key
2. **Verify cost calculation accuracy** — compare estimated costs against
   reconciled costs from the `/costs` endpoint
3. **Test enforcement** — verify API key restriction actually blocks spend
   (may need to use the scopes-based approach once OpenAI exposes it)

### Nice-to-haves
4. **Auto-refresh pricing** — fetch current rates from OpenAI
5. **Historical trend display** — simple ASCII chart of spend over time
6. **Timezone support** — let users configure display/limit timezone
