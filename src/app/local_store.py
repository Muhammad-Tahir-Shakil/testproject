"""SQLite persistence for the local browser dashboard.

This is the *operational* store, not the audit trail: it holds the job as the
dispatcher entered it, because an operator cannot triage what they cannot read.
``StoreAuditSink`` applies the same redaction as the S3 sink, so the two
deliberately differ. ``docs/security.md`` sets out the boundary.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

from .models import DecisionSnapshot

# Rows returned to the dashboard for the rolling views. Counts are queried
# separately with COUNT(*) so the metric cards report real totals rather than
# the length of a truncated page.
RECENT_RECOMMENDATIONS = 5
RECENT_EVENTS = 20
RECENT_AUDIT = 20


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalStore:
    """Small file-backed store; no external database is required locally."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = Lock()
        self._initialized = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open, commit or roll back, and always close.

        The previous version used ``with sqlite3.connect(...)`` directly, which
        commits but never closes, leaking a handle on every request.
        """

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            # WAL keeps the dashboard's reads from blocking a concurrent write.
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the schema once per process rather than on every call."""

        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS vendors (
                        vendor_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS recommendations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_recommendations_job
                        ON recommendations(job_id, id DESC);
                    CREATE TABLE IF NOT EXISTS active_decisions (
                        job_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS decision_revisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        decision_version INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(job_id, decision_version)
                    );
                    CREATE INDEX IF NOT EXISTS idx_decisions_job
                        ON decision_revisions(job_id, decision_version DESC);
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        action TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    """
                )
            self._initialized = True

    def set_setting(self, key: str, value: Any) -> None:
        self.initialize()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, json.dumps(value)),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row["value"]) if row else default

    def save_job(self, payload: dict[str, Any]) -> None:
        self.initialize()
        now = utc_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(job_id, payload, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (payload["job_id"], json.dumps(payload), now),
            )

    def save_vendor(self, payload: dict[str, Any]) -> None:
        self.initialize()
        now = utc_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO vendors(vendor_id, payload, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(vendor_id) DO UPDATE SET
                    payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (payload["vendor_id"], json.dumps(payload), now),
            )

    def save_recommendation(self, job_id: str, payload: dict[str, Any]) -> None:
        self.initialize()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO recommendations(job_id, payload, created_at) "
                "VALUES(?, ?, ?)",
                (job_id, json.dumps(payload), utc_iso()),
            )

    def latest_recommendation(self, job_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM recommendations
                WHERE job_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def vendor_name(self, vendor_id: str) -> str | None:
        """Look the vendor up by primary key instead of scanning the table."""

        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM vendors WHERE vendor_id = ?", (vendor_id,)
            ).fetchone()
        return json.loads(row["payload"]).get("name") if row else None

    def get_decision(self, job_id: str) -> DecisionSnapshot | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM active_decisions WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return DecisionSnapshot.model_validate_json(row["payload"]) if row else None

    def save_decision(self, snapshot: DecisionSnapshot) -> None:
        self.initialize()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO active_decisions(job_id, payload, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (
                    snapshot.job_id,
                    snapshot.model_dump_json(),
                    snapshot.updated_at.isoformat(),
                ),
            )
            for revision in snapshot.revisions:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO decision_revisions(
                        job_id, decision_version, payload, created_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (
                        snapshot.job_id,
                        revision.decision_version,
                        revision.model_dump_json(),
                        revision.recorded_at.isoformat(),
                    ),
                )

    def save_event(
        self, event_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        self.initialize()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events(event_id, event_type, payload, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (event_id, event_type, json.dumps(payload), utc_iso()),
            )

    def save_audit(self, action: str, payload: dict[str, Any]) -> None:
        self.initialize()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO audit(action, payload, created_at) VALUES(?, ?, ?)",
                (action, json.dumps(payload), utc_iso()),
            )

    def dashboard(self, model_metadata: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        setup_ready = self.get_setting("setup_ready", False)
        with self._connect() as connection:
            jobs = [
                json.loads(row["payload"])
                for row in connection.execute(
                    "SELECT payload FROM jobs ORDER BY updated_at DESC"
                )
            ]
            vendors = [
                json.loads(row["payload"])
                for row in connection.execute(
                    "SELECT payload FROM vendors ORDER BY updated_at DESC"
                )
            ]
            recommendations = [
                json.loads(row["payload"])
                for row in connection.execute(
                    "SELECT payload FROM recommendations ORDER BY id DESC LIMIT ?",
                    (RECENT_RECOMMENDATIONS,),
                )
            ]
            events = [
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload"]),
                    "created_at": row["created_at"],
                }
                for row in connection.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (RECENT_EVENTS,)
                )
            ]
            audit = [
                {
                    "action": row["action"],
                    "payload": json.loads(row["payload"]),
                    "created_at": row["created_at"],
                }
                for row in connection.execute(
                    "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (RECENT_AUDIT,)
                )
            ]
            decisions = [
                json.loads(row["payload"])
                for row in connection.execute(
                    "SELECT payload FROM active_decisions ORDER BY updated_at DESC"
                )
            ]
            # Real totals, not len() of a truncated page.
            counts = {
                table: connection.execute(
                    f"SELECT COUNT(*) AS total FROM {table}"  # noqa: S608 - fixed names
                ).fetchone()["total"]
                for table in ("jobs", "vendors", "recommendations", "events", "audit")
            }

        return {
            "setup_ready": setup_ready,
            "database": str(self.db_path),
            "jobs": jobs,
            "vendors": vendors,
            "recommendations": recommendations,
            "events": events,
            "audit": audit,
            "decisions": decisions,
            "model": model_metadata,
            "counts": counts,
            "page_sizes": {
                "recommendations": RECENT_RECOMMENDATIONS,
                "events": RECENT_EVENTS,
                "audit": RECENT_AUDIT,
            },
        }

    def reset(self) -> None:
        """Delete the database and its WAL sidecars."""

        with self._lock:
            self._initialized = False
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(self.db_path) + suffix)
                if candidate.exists():
                    candidate.unlink()
