# openai-watchdog

A CLI tool to monitor OpenAI API usage and costs, enforce per-key spending
limits, and alert you before bills get out of control.

## Features

- **Rolling spend limits** — set hourly and daily dollar limits per API key group
- **Soft + hard enforcement** — email warnings at the soft limit; automatically
  restrict keys at the hard limit (150% of soft by default)
- **Periodic polling** — runs every 10 minutes (configurable), stores costs in
  a local SQLite database, and checks rolling 1-hour / 24-hour windows
- **Top spenders report** — after each poll, prints the 5 most expensive API keys
- **YAML config** — one file defines key groups, limits, alerts, and SMTP settings
- **CSV/JSON export** — download usage reports for offline analysis
- **50+ model pricing** — built-in pricing for GPT-5.x, GPT-4.1, GPT-4o,
  o-series, codex, embeddings, images, audio, and more

## Requirements

- Python 3.11+
- An **OpenAI Admin API key** (not a regular project key)

The Organization Usage API requires an Admin key.  Create one at:
https://platform.openai.com/settings/organization/admin-keys

## Installation

```bash
# Clone and install with uv
git clone https://github.com/davidbartonau/openai-watchdog.git
cd openai-watchdog
uv sync

# Or install dependencies manually
pip install openai>=1.0.0 pyyaml>=6.0
```

## Quick start

```bash
# Set your admin key
export OPENAI_ADMIN_KEY="sk-admin-..."

# See what you've spent in the last 24 hours
openai-watchdog usage

# See reconciled billing costs for the last 7 days
openai-watchdog costs

# Show built-in pricing table
openai-watchdog prices
```

## Configuration

Copy the example config and edit it:

```bash
cp watchdog.example.yaml watchdog.yaml
```

### Example `watchdog.yaml`

```yaml
admin_key_env: OPENAI_ADMIN_KEY

poll:
  interval_seconds: 600    # 10 minutes
  db_path: watchdog.db     # SQLite database file

key_groups:
  team-alpha:
    api_key_ids:
      - sk-proj-abc123...
      - sk-proj-def456...
    owner_email: alice@example.com
    limits:
      hourly: 20.0           # soft limit: $20/hour
      daily: 30.0            # soft limit: $30/day
      hard_multiplier: 1.5   # hard limit = soft * 1.5

  team-beta:
    api_key_ids:
      - sk-proj-ghi789...
    owner_email: bob@example.com
    limits:
      hourly: 10.0
      daily: 50.0

alerts:
  stdout: true
  # webhook_url: https://hooks.slack.com/services/...
  email:
    smtp_host: smtp.gmail.com
    smtp_port: 587
    from_addr: watchdog@example.com
    username: watchdog@example.com
    password_env: SMTP_PASSWORD    # reads password from this env var
    cc_addrs:                      # CC these addresses on every alert
      - manager@example.com
      - billing@example.com

export:
  format: csv
  output_dir: ./reports
```

### Config reference

| Field | Description | Default |
|---|---|---|
| `admin_key_env` | Env var containing the Admin API key | `OPENAI_ADMIN_KEY` |
| `poll.interval_seconds` | Seconds between poll cycles | `600` (10 min) |
| `poll.db_path` | Path to the SQLite database | `watchdog.db` |
| `key_groups.<name>.api_key_ids` | List of API key IDs in this group | (required) |
| `key_groups.<name>.owner_email` | Email for soft-limit notifications | (optional) |
| `key_groups.<name>.limits.hourly` | Soft hourly spend limit in USD | (optional) |
| `key_groups.<name>.limits.daily` | Soft daily spend limit in USD | (optional) |
| `key_groups.<name>.limits.hard_multiplier` | Hard limit = soft * this | `1.5` |
| `alerts.stdout` | Print alerts to stderr | `true` |
| `alerts.webhook_url` | POST JSON alerts to this URL | (optional) |
| `alerts.email.smtp_host` | SMTP server hostname | (optional) |
| `alerts.email.smtp_port` | SMTP server port | `587` |
| `alerts.email.from_addr` | From address for alert emails | (optional) |
| `alerts.email.username` | SMTP login username | (optional) |
| `alerts.email.password_env` | Env var containing SMTP password | `SMTP_PASSWORD` |
| `alerts.email.cc_addrs` | List of CC addresses for all alert emails | `[]` |
| `export.format` | Export format: `csv` or `json` | `csv` |
| `export.output_dir` | Directory for exported reports | `./reports` |

## Polling and limits

### How it works

The poller runs on a schedule (every 10 minutes by default) and:

1. Queries the OpenAI Usage API for spend **since the last poll**
2. Stores the dollar cost per group and per key in a local **SQLite database**
3. Computes **rolling 1-hour and 24-hour totals** from the stored data
4. Compares against configured limits and takes action if exceeded
5. Prints the **top 5 most expensive keys** in the last 24 hours

### First run behavior

On the very first run (no database history), the poller looks back **24 hours**
to seed the daily rolling window.  Hourly limits are **skipped on the first
run** because all 24 hours of cost are loaded as a single data point — the
hourly window would be misleadingly inflated.  Hourly limits activate on the
second and subsequent runs once we have granular per-interval data.

### Running with cron

For production use, run the poller as a cron job:

```bash
# Every 10 minutes
*/10 * * * * cd /path/to/openai-watchdog && openai-watchdog poll --once >> /var/log/watchdog.log 2>&1
```

The `--once` flag runs a single poll cycle and exits.  Exit codes:
- `0` — all groups within limits
- `1` — at least one soft limit exceeded
- `2` — at least one hard limit exceeded (keys were restricted)

### Running as a daemon

Alternatively, run it as a long-lived process:

```bash
openai-watchdog poll
# Polls every 10 minutes (or --interval N seconds)
# Ctrl-C or SIGTERM to stop gracefully
```

### Checking status without polling

```bash
openai-watchdog status
```

This reads the SQLite database and shows current rolling costs without
making any API calls.  Useful for quick checks between poll cycles.

## Limit logic

For each key group, limits are checked against rolling windows:

| Check | Window | Threshold | Action |
|---|---|---|---|
| Soft hourly | Last 1h | `limits.hourly` | Email owner + alert |
| Soft daily | Last 24h | `limits.daily` | Email owner + alert |
| Hard hourly | Last 1h | `hourly * 1.5` | **Restrict keys** + email + alert |
| Hard daily | Last 24h | `daily * 1.5` | **Restrict keys** + email + alert |

**Example:** `hourly: 20.0, daily: 30.0`
- Soft limits: $20/hour, $30/day
- Hard limits: $30/hour (150%), $45/day (150%)

## Email alerts

Email alerts are sent to the `owner_email` configured on each key group when a
soft or hard limit is breached.  To enable email alerts, configure the SMTP
settings in the `alerts.email` section of `watchdog.yaml`:

```yaml
alerts:
  email:
    smtp_host: smtp.gmail.com      # your SMTP server
    smtp_port: 587                  # TLS port (587 is standard)
    from_addr: watchdog@example.com # the "From" address on alert emails
    username: watchdog@example.com  # SMTP login username
    password_env: SMTP_PASSWORD     # env var containing the SMTP password
    cc_addrs:                       # CC these addresses on every alert
      - manager@example.com
      - billing@example.com
```

Then set the password in your environment:

```bash
export SMTP_PASSWORD="your-smtp-password"
```

Each key group needs an `owner_email` to receive alerts:

```yaml
key_groups:
  team-alpha:
    owner_email: alice@example.com  # soft limit emails go here
```

**What gets emailed:**

- **Soft limit breach:** a warning email telling the owner their spend is over
  the limit, and what the hard limit is (at which point keys get restricted).
- **Hard limit breach:** a notification that keys have been restricted, with
  instructions to contact an admin to restore access.

Alerts have a **1-hour cooldown** per group per alert type — you won't get
spammed with the same alert every 10 minutes.

## Key restriction (hard limit enforcement)

When a hard limit is breached, the watchdog uses the OpenAI Admin API to
restrict each API key in the group:

- Calls `POST /v1/organization/api_keys/{key_id}` to rename the key with a
  `[RESTRICTED by watchdog]` prefix
- Sends an email to the key owner notifying them of the restriction
- Posts to the configured webhook URL

To restore a restricted key, an admin can use the `restore_api_key()` function
or manually rename the key in the OpenAI dashboard.

## Database

The poller stores all data in a local **SQLite** database (`watchdog.db` by
default).  No external database server is needed.  The database is created
automatically on first run.

### Tables

| Table | Purpose |
|---|---|
| `poll_runs` | Tracks when each poll started and finished |
| `usage_records` | Per-group dollar costs from each poll interval |
| `key_costs` | Per-key dollar costs for the top-spenders report |
| `alerts_sent` | Deduplication log for alerts (1-hour cooldown) |

Data older than 7 days is automatically pruned after each poll.

### Timezone

All timestamps in the database and output are **UTC**.  The OpenAI Usage
API operates in UTC, and the poller stores raw Unix timestamps.  Display
output is formatted as `YYYY-MM-DD HH:MM UTC`.

## All CLI commands

```
openai-watchdog <command> [options]
```

| Command | Description |
|---|---|
| `usage` | Show raw usage across all API endpoint types |
| `costs` | Show reconciled billing data (authoritative for invoices) |
| `prices` | Display built-in model pricing table |
| `groups` | Show usage aggregated by configured key groups |
| `watch` | One-shot rate-limit check against the live API |
| `poll` | Periodic polling with rolling limits and enforcement |
| `status` | Show current rolling costs from the database |
| `export` | Export raw usage data to CSV or JSON |

### Global options

All commands accept:
- `--config <path>` — path to watchdog.yaml (default: auto-discovered)

### `openai-watchdog poll`

```
openai-watchdog poll [--once] [--interval N] [--db PATH] [--config PATH]
```

| Flag | Description | Default |
|---|---|---|
| `--once` | Run a single poll cycle and exit (for cron) | continuous |
| `--interval N` | Poll interval in seconds | from config or 600 |
| `--db PATH` | SQLite database path | from config or `watchdog.db` |

### `openai-watchdog status`

```
openai-watchdog status [--db PATH] [--config PATH]
```

Shows rolling 1h and 24h costs per group, with limit status.

### `openai-watchdog usage`

```
openai-watchdog usage [--since TIME] [--until TIME] [--group-by DIMS]
                      [--types TYPES] [--bucket-width 1m|1h|1d]
```

| Flag | Description | Default |
|---|---|---|
| `--since` | Start time (`24h`, `7d`, unix ts, ISO date) | `24h` |
| `--until` | End time (same formats) | now |
| `--group-by` | Comma-separated dimensions | `api_key_id,model` |
| `--types` | Bucket types to fetch | all |
| `--bucket-width` | Time granularity | `1d` |

### `openai-watchdog costs`

```
openai-watchdog costs [--since TIME] [--until TIME] [--group-by DIMS]
```

Shows reconciled billing data.  Default: last 7 days.

### `openai-watchdog groups`

```
openai-watchdog groups [--since TIME] [--until TIME]
                       [--export] [--export-format csv|json] [--export-dir DIR]
```

Shows usage per configured key group with per-model cost breakdown.

### `openai-watchdog export`

```
openai-watchdog export [--since TIME] [--until TIME] [--types TYPES]
                       [--format csv|json] [--output-dir DIR]
```

Exports raw usage data to a timestamped file.

## Example poll output

```
[poll] 2026-02-07 14:30 UTC
[poll] window: 2026-02-07 14:20 UTC -> 2026-02-07 14:30 UTC  (10 min)
[poll] team-alpha: $2.340000 in this interval
[poll] team-beta: $0.850000 in this interval
[poll] team-alpha:  1h=$8.2300/$20.00  24h=$24.5600/$30.00  [OK]
[poll] team-beta:   1h=$3.1200/$10.00  24h=$12.4500/$50.00  [OK]
[poll] Top 5 keys by cost (24h):
  1. sk-proj-abc1...f456  $12.3400  (team-alpha)
  2. sk-proj-def4...g789  $8.2200   (team-alpha)
  3. sk-proj-ghi7...h012  $6.1500   (team-beta)
  4. sk-proj-jkl0...i345  $4.0000   (team-alpha)
  5. sk-proj-mno3...j678  $2.8000   (team-beta)
[poll] All 2 group(s) within limits
```

## Project structure

```
openai_watchdog/
  __init__.py          # Package init
  cli.py               # CLI entry point and argument parsing
  config.py            # YAML configuration loader and schema
  db.py                # SQLite database layer
  enforcement.py       # Soft/hard limit enforcement (email, key restriction)
  export.py            # CSV/JSON report export
  monitor.py           # Key-group usage aggregation
  poller.py            # Periodic poll cycle logic
  pricing.py           # Built-in model pricing tables
  usage.py             # OpenAI Organization Usage & Costs API client
watchdog.example.yaml  # Annotated example configuration
pyproject.toml         # Project metadata and dependencies
```
