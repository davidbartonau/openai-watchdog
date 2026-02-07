"""SQLite storage for usage tracking and poll state.

Tables:
- ``poll_runs``       — when the poller last ran successfully
- ``usage_records``   — per-group dollar costs recorded each poll interval
- ``alerts_sent``     — deduplication log so we don't spam alerts
"""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_DB_PATH = "watchdog.db"


@dataclass
class UsageRecord:
    """A single cost sample captured during one poll cycle."""

    id: Optional[int]
    group_name: str
    poll_ts: int          # unix timestamp of the poll run
    period_start: int     # start of the usage window we queried
    period_end: int       # end of the usage window we queried
    cost_usd: float       # estimated dollar cost in that window


@dataclass
class PollRun:
    id: Optional[int]
    started_at: int
    finished_at: int


class WatchdogDB:
    """Thin wrapper around a SQLite database for watchdog state."""

    def __init__(self, path: str = DEFAULT_DB_PATH):
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        return self._conn

    def _create_tables(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS poll_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at  INTEGER NOT NULL,
                finished_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage_records (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                group_name    TEXT    NOT NULL,
                poll_ts       INTEGER NOT NULL,
                period_start  INTEGER NOT NULL,
                period_end    INTEGER NOT NULL,
                cost_usd      REAL    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_usage_group_ts
                ON usage_records (group_name, poll_ts);

            CREATE TABLE IF NOT EXISTS alerts_sent (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_name  TEXT    NOT NULL,
                alert_type  TEXT    NOT NULL,   -- 'soft_hourly', 'soft_daily', 'hard_hourly', 'hard_daily'
                sent_at     INTEGER NOT NULL,
                cost_usd    REAL    NOT NULL,
                limit_usd   REAL    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_alerts_group_type
                ON alerts_sent (group_name, alert_type, sent_at);

            CREATE TABLE IF NOT EXISTS email_batches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_type  TEXT    NOT NULL,   -- 'soft' or 'hard'
                sent_at     INTEGER NOT NULL,
                key_ids     TEXT    NOT NULL    -- JSON array of key IDs
            );

            CREATE INDEX IF NOT EXISTS idx_email_batches_type_time
                ON email_batches (batch_type, sent_at);

            CREATE TABLE IF NOT EXISTS key_costs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key_id    TEXT    NOT NULL,
                group_name    TEXT    NOT NULL,
                poll_ts       INTEGER NOT NULL,
                cost_usd      REAL    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_key_costs_ts
                ON key_costs (poll_ts);
        """)

    # ------------------------------------------------------------------
    # Poll runs
    # ------------------------------------------------------------------

    def record_poll_start(self) -> int:
        """Record that a poll is starting.  Returns the started_at timestamp."""
        ts = int(time.time())
        self.conn.execute(
            "INSERT INTO poll_runs (started_at, finished_at) VALUES (?, 0)",
            (ts,),
        )
        self.conn.commit()
        return ts

    def record_poll_end(self, started_at: int) -> None:
        ts = int(time.time())
        self.conn.execute(
            "UPDATE poll_runs SET finished_at = ? WHERE started_at = ?",
            (ts, started_at),
        )
        self.conn.commit()

    def last_successful_poll_ts(self) -> Optional[int]:
        """Return the finished_at timestamp of the most recent successful poll."""
        row = self.conn.execute(
            "SELECT finished_at FROM poll_runs WHERE finished_at > 0 "
            "ORDER BY finished_at DESC LIMIT 1",
        ).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Usage records
    # ------------------------------------------------------------------

    def store_usage(
        self,
        group_name: str,
        poll_ts: int,
        period_start: int,
        period_end: int,
        cost_usd: float,
    ) -> None:
        self.conn.execute(
            "INSERT INTO usage_records "
            "(group_name, poll_ts, period_start, period_end, cost_usd) "
            "VALUES (?, ?, ?, ?, ?)",
            (group_name, poll_ts, period_start, period_end, cost_usd),
        )
        self.conn.commit()

    def rolling_cost(self, group_name: str, window_seconds: int) -> float:
        """Sum of cost_usd for *group_name* within the last *window_seconds*.

        Uses ``poll_ts`` (when we captured the data) to define the window —
        this means the rolling window is "wall-clock time of data capture",
        which handles overlapping or gapped poll intervals gracefully.
        """
        cutoff = int(time.time()) - window_seconds
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_records "
            "WHERE group_name = ? AND poll_ts >= ?",
            (group_name, cutoff),
        ).fetchone()
        return row[0]

    def rolling_cost_hourly(self, group_name: str) -> float:
        return self.rolling_cost(group_name, 3600)

    def rolling_cost_daily(self, group_name: str) -> float:
        return self.rolling_cost(group_name, 86400)

    def total_rolling_cost(self, window_seconds: int) -> float:
        """Sum of cost_usd across ALL groups within the last *window_seconds*."""
        cutoff = int(time.time()) - window_seconds
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_records "
            "WHERE poll_ts >= ?",
            (cutoff,),
        ).fetchone()
        return row[0]

    def interval_cost(self, poll_ts: int) -> float:
        """Sum of cost_usd across ALL groups for a specific poll timestamp."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_records "
            "WHERE poll_ts = ?",
            (poll_ts,),
        ).fetchone()
        return row[0]

    # ------------------------------------------------------------------
    # Per-key costs
    # ------------------------------------------------------------------

    def store_key_cost(
        self,
        api_key_id: str,
        group_name: str,
        poll_ts: int,
        cost_usd: float,
    ) -> None:
        self.conn.execute(
            "INSERT INTO key_costs (api_key_id, group_name, poll_ts, cost_usd) "
            "VALUES (?, ?, ?, ?)",
            (api_key_id, group_name, poll_ts, cost_usd),
        )
        self.conn.commit()

    def top_keys_by_cost(
        self,
        window_seconds: int = 86400,
        limit: int = 5,
    ) -> list[tuple[str, str, float]]:
        """Return the top N most expensive API keys in the rolling window.

        Returns list of (api_key_id, group_name, total_cost_usd).
        """
        cutoff = int(time.time()) - window_seconds
        rows = self.conn.execute(
            "SELECT api_key_id, group_name, SUM(cost_usd) as total "
            "FROM key_costs WHERE poll_ts >= ? "
            "GROUP BY api_key_id "
            "ORDER BY total DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def rolling_key_cost(self, api_key_id: str, window_seconds: int) -> float:
        """Sum of cost_usd for a specific API key within the last *window_seconds*."""
        cutoff = int(time.time()) - window_seconds
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM key_costs "
            "WHERE api_key_id = ? AND poll_ts >= ?",
            (api_key_id, cutoff),
        ).fetchone()
        return row[0]

    def rolling_key_cost_hourly(self, api_key_id: str) -> float:
        return self.rolling_key_cost(api_key_id, 3600)

    def rolling_key_cost_daily(self, api_key_id: str) -> float:
        return self.rolling_key_cost(api_key_id, 86400)

    def all_keys_with_costs(
        self,
        window_seconds: int,
    ) -> list[tuple[str, str, float]]:
        """Return all keys with their rolling costs.

        Returns list of (api_key_id, group_name, total_cost_usd).
        """
        cutoff = int(time.time()) - window_seconds
        rows = self.conn.execute(
            "SELECT api_key_id, group_name, SUM(cost_usd) as total "
            "FROM key_costs WHERE poll_ts >= ? "
            "GROUP BY api_key_id "
            "ORDER BY total DESC",
            (cutoff,),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    # ------------------------------------------------------------------
    # Email batch tracking (for smart cooldown)
    # ------------------------------------------------------------------

    def record_email_batch(
        self,
        batch_type: str,  # 'soft' or 'hard'
        key_ids: list[str],
    ) -> None:
        """Record that an email was sent with the given key IDs."""
        import json
        self.conn.execute(
            "INSERT INTO email_batches (batch_type, sent_at, key_ids) VALUES (?, ?, ?)",
            (batch_type, int(time.time()), json.dumps(key_ids)),
        )
        self.conn.commit()

    def get_last_batch_keys(
        self,
        batch_type: str,
        cooldown_seconds: int = 3600,
    ) -> set[str]:
        """Get the key IDs from the most recent email batch within the cooldown."""
        import json
        cutoff = int(time.time()) - cooldown_seconds
        row = self.conn.execute(
            "SELECT key_ids FROM email_batches "
            "WHERE batch_type = ? AND sent_at >= ? "
            "ORDER BY sent_at DESC LIMIT 1",
            (batch_type, cutoff),
        ).fetchone()
        if row:
            return set(json.loads(row[0]))
        return set()

    def should_skip_email(
        self,
        batch_type: str,
        current_key_ids: list[str],
        cooldown_seconds: int = 3600,
    ) -> bool:
        """Check if we should skip sending an email.

        Returns True if:
        - There was a recent email batch (within cooldown)
        - All current keys are a subset of the keys in that batch

        This means: if we alerted A, B, C and now A, B are over, skip.
        But if A, B, D are over, send because D is new.
        """
        if not current_key_ids:
            return True

        last_keys = self.get_last_batch_keys(batch_type, cooldown_seconds)
        if not last_keys:
            # No recent batch, don't skip
            return False

        current_set = set(current_key_ids)
        # Skip if all current keys were in the last batch
        return current_set.issubset(last_keys)

    # ------------------------------------------------------------------
    # Alert deduplication
    # ------------------------------------------------------------------

    def record_alert(
        self,
        group_name: str,
        alert_type: str,
        cost_usd: float,
        limit_usd: float,
    ) -> None:
        self.conn.execute(
            "INSERT INTO alerts_sent "
            "(group_name, alert_type, sent_at, cost_usd, limit_usd) "
            "VALUES (?, ?, ?, ?, ?)",
            (group_name, alert_type, int(time.time()), cost_usd, limit_usd),
        )
        self.conn.commit()

    def was_alert_sent_recently(
        self,
        group_name: str,
        alert_type: str,
        cooldown_seconds: int = 3600,
    ) -> bool:
        """Check if an alert of this type was already sent within the cooldown."""
        cutoff = int(time.time()) - cooldown_seconds
        row = self.conn.execute(
            "SELECT COUNT(*) FROM alerts_sent "
            "WHERE group_name = ? AND alert_type = ? AND sent_at >= ?",
            (group_name, alert_type, cutoff),
        ).fetchone()
        return row[0] > 0

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def prune_old_records(self, max_age_seconds: int = 7 * 86400) -> int:
        """Delete usage records and alerts older than *max_age_seconds*."""
        cutoff = int(time.time()) - max_age_seconds
        cur = self.conn.execute(
            "DELETE FROM usage_records WHERE poll_ts < ?", (cutoff,)
        )
        deleted = cur.rowcount
        self.conn.execute(
            "DELETE FROM key_costs WHERE poll_ts < ?", (cutoff,)
        )
        self.conn.execute(
            "DELETE FROM alerts_sent WHERE sent_at < ?", (cutoff,)
        )
        self.conn.execute(
            "DELETE FROM email_batches WHERE sent_at < ?", (cutoff,)
        )
        self.conn.execute(
            "DELETE FROM poll_runs WHERE finished_at < ? AND finished_at > 0",
            (cutoff,),
        )
        self.conn.commit()
        return deleted
