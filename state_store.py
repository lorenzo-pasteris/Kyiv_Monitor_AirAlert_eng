"""
Durable persistence layer for Kyiv Monitor: SQLite-backed operational state,
the NORMAL-mode message queue, ALERT-feed cursors/dedup claims, hourly
category statistics, and the Telethon single-session file lock.

All functions here read/write module-level state (`CATEGORY_STATS_DB_PATH`,
`stats_db_ready`). Callers — including monitor.py — must go through the
`state_store` module object (`import state_store`, then `state_store.thing`)
rather than importing individual names, so that tests can monkeypatch
`state_store.CATEGORY_STATS_DB_PATH` / `state_store.stats_db_ready` and have
every function in this module observe the patched value.
"""
import fcntl
import hashlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from text_processing import alert_feed_cursor_key, normalize_alert_for_dedup, utc_iso

CATEGORY_STATS_DB_PATH = os.environ.get(
    "CATEGORY_STATS_DB_PATH", "/data/kyiv_monitor_category_stats.sqlite3"
)
TELETHON_SESSION_LOCK_PATH = f"{CATEGORY_STATS_DB_PATH}.telethon.lock"
NORMAL_MESSAGE_RETENTION_DAYS = 7

stats_db_ready = False


def initialize_stats_db():
    """Create the persistent statistics and NORMAL-message store."""
    try:
        db_dir = os.path.dirname(CATEGORY_STATS_DB_PATH)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS hourly_category_stats (
                    run_at TEXT NOT NULL,
                    category TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    received INTEGER NOT NULL,
                    valid INTEGER NOT NULL,
                    PRIMARY KEY (run_at, category, channel)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS hourly_classifications (
                    run_at TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    category TEXT NOT NULL,
                    preview TEXT NOT NULL,
                    PRIMARY KEY (run_at, message_id, category)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS normal_messages (
                    channel TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    message_at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    processed_at TEXT,
                    PRIMARY KEY (channel, message_id)
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS normal_messages_status_time
                   ON normal_messages (status, message_at)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS source_cursors (
                    channel TEXT PRIMARY KEY,
                    last_message_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS operational_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS alert_feed_deliveries (
                    channel TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (channel, message_id, fingerprint)
                )"""
            )
        print(f"[STATS DB] ready path={CATEGORY_STATS_DB_PATH}")
        return True
    except Exception as exc:
        print(f"[STATS DB ERROR] persistence unavailable: {type(exc).__name__}: {exc}")
        return False


def acquire_telethon_session_lock(path=None, *, blocking=True):
    """Prevent two production containers from opening the same Telegram session."""
    if path is None:
        path = TELETHON_SESSION_LOCK_PATH
    lock_dir = os.path.dirname(path)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        print(f"[TELETHON SESSION LOCK] waiting path={path}")
        fcntl.flock(handle, operation)
    except Exception:
        handle.close()
        raise
    print(f"[TELETHON SESSION LOCK] acquired path={path}")
    return handle


def persist_operational_state(key, value):
    """Persist small restart-sensitive state without storing credentials."""
    if not stats_db_ready:
        return False
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH, timeout=5) as connection:
            connection.execute(
                """INSERT INTO operational_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value, updated_at = excluded.updated_at""",
                (key, str(value), utc_iso()),
            )
        return True
    except Exception as exc:
        print(f"[STATE DB ERROR] key={key}: {type(exc).__name__}: {exc}")
        return False


def load_operational_state(key):
    if not stats_db_ready:
        return None
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH, timeout=5) as connection:
            row = connection.execute(
                "SELECT value FROM operational_state WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None
    except Exception as exc:
        print(f"[STATE DB ERROR] read key={key}: {type(exc).__name__}: {exc}")
        return None


def persist_trigger_observation(state, message_id, message_at):
    """Record the canonical trigger event used to reconstruct the effective mode."""
    if not stats_db_ready:
        return False
    updated_at = utc_iso()
    values = (
        ("telegram_alert_state", "1" if state else "0", updated_at),
        ("telegram_alert_message_id", str(message_id), updated_at),
        ("telegram_alert_message_at", utc_iso(message_at), updated_at),
    )
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH, timeout=5) as connection:
            connection.executemany(
                """INSERT INTO operational_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value, updated_at = excluded.updated_at""",
                values,
            )
        return True
    except Exception as exc:
        print(f"[STATE DB ERROR] trigger observation: {type(exc).__name__}: {exc}")
        return False


def get_alert_feed_cursor(channel):
    value = load_operational_state(alert_feed_cursor_key(channel))
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def set_alert_feed_cursor(channel, message_id):
    return persist_operational_state(alert_feed_cursor_key(channel), int(message_id))


def claim_alert_feed_delivery(channel, message_id, text):
    """Atomically reserve one source-message version across worker restarts."""
    if not stats_db_ready:
        return True
    fingerprint = hashlib.sha256(normalize_alert_for_dedup(text).encode()).hexdigest()
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH, timeout=5) as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO alert_feed_deliveries
                   (channel, message_id, fingerprint, claimed_at)
                   VALUES (?, ?, ?, ?)""",
                (channel, int(message_id), fingerprint, utc_iso()),
            )
        return cursor.rowcount == 1
    except Exception as exc:
        print(f"[ALERT CLAIM DB ERROR] @{channel} id={message_id}: {type(exc).__name__}: {exc}")
        return True


def release_alert_feed_delivery(channel, message_id, text):
    """Release a failed reservation so the same message version can retry."""
    if not stats_db_ready:
        return
    fingerprint = hashlib.sha256(normalize_alert_for_dedup(text).encode()).hexdigest()
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH, timeout=5) as connection:
            connection.execute(
                """DELETE FROM alert_feed_deliveries
                   WHERE channel = ? AND message_id = ? AND fingerprint = ?""",
                (channel, int(message_id), fingerprint),
            )
    except Exception as exc:
        print(f"[ALERT CLAIM DB ERROR] release @{channel} id={message_id}: {type(exc).__name__}: {exc}")


def is_stale_alert_feed_message(channel, message_id):
    """True when the cursor-based pipeline has already covered this Telegram message id.

    The live Telethon listener and the 5-second poller ingest the same feed. The
    180-second in-memory dedup cannot catch a live event replayed minutes later
    (typical after a reconnection), so the durable cursor is the authoritative
    guard. A missing cursor (0) keeps the previous behaviour: nothing is stale.
    """
    return int(message_id) <= get_alert_feed_cursor(channel)


def persist_normal_message(channel, message_id, message_at, text):
    """Persist one live NORMAL message idempotently; return False only on storage failure."""
    if not stats_db_ready:
        return False
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO normal_messages
                   (channel, message_id, message_at, text, status)
                   VALUES (?, ?, ?, ?, 'pending')""",
                (channel, int(message_id), utc_iso(message_at), text[:800]),
            )
        return True
    except Exception as exc:
        print(f"[NORMAL DB ERROR] live insert failed @{channel}: {type(exc).__name__}: {exc}")
        return False


def get_source_cursor(channel):
    if not stats_db_ready:
        return None
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            row = connection.execute(
                "SELECT last_message_id FROM source_cursors WHERE channel = ?",
                (channel,),
            ).fetchone()
        return int(row[0]) if row else None
    except Exception as exc:
        print(f"[NORMAL DB ERROR] cursor read failed @{channel}: {type(exc).__name__}: {exc}")
        return None


def persist_history_batch(channel, messages, last_message_id):
    """Atomically persist a history batch and its source cursor."""
    if not stats_db_ready:
        return False
    try:
        now = utc_iso()
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            for message in messages:
                connection.execute(
                    """INSERT OR IGNORE INTO normal_messages
                       (channel, message_id, message_at, text, status)
                       VALUES (?, ?, ?, ?, 'pending')""",
                    (
                        channel,
                        int(message["message_id"]),
                        message["message_at"],
                        message["text"][:800],
                    ),
                )
            if last_message_id is not None:
                connection.execute(
                    """INSERT INTO source_cursors (channel, last_message_id, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(channel) DO UPDATE SET
                           last_message_id = excluded.last_message_id,
                           updated_at = excluded.updated_at""",
                    (channel, int(last_message_id), now),
                )
        return True
    except Exception as exc:
        print(f"[NORMAL DB ERROR] history batch failed @{channel}: {type(exc).__name__}: {exc}")
        return False


def load_pending_normal_messages():
    """Load the durable NORMAL queue in chronological order."""
    if not stats_db_ready:
        return []
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            rows = connection.execute(
                """SELECT channel, message_id, message_at, text
                   FROM normal_messages
                   WHERE status = 'pending'
                   ORDER BY message_at, channel, message_id"""
            ).fetchall()
        return [
            {
                "channel": channel,
                "message_id": int(message_id),
                "message_at": message_at,
                "text": text,
            }
            for channel, message_id, message_at, text in rows
        ]
    except Exception as exc:
        print(f"[NORMAL DB ERROR] pending read failed: {type(exc).__name__}: {exc}")
        return []


def mark_normal_messages_processed(message_keys):
    """Mark exactly the delivered snapshot as processed, leaving later arrivals pending."""
    if not stats_db_ready or not message_keys:
        return
    try:
        processed_at = utc_iso()
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            connection.executemany(
                """UPDATE normal_messages
                   SET status = 'processed', processed_at = ?
                   WHERE channel = ? AND message_id = ? AND status = 'pending'""",
                [(processed_at, channel, int(message_id)) for channel, message_id in message_keys],
            )
            cutoff = utc_iso(datetime.now(timezone.utc) - timedelta(days=NORMAL_MESSAGE_RETENTION_DAYS))
            connection.execute(
                """DELETE FROM normal_messages
                   WHERE status != 'pending' AND processed_at < ?""",
                (cutoff,),
            )
    except Exception as exc:
        print(f"[NORMAL DB ERROR] completion write failed: {type(exc).__name__}: {exc}")


def discard_pending_normal_messages(reason):
    """Preserve the original rule that an alert discards accumulated NORMAL material."""
    if not stats_db_ready:
        return
    try:
        processed_at = utc_iso()
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            changed = connection.execute(
                """UPDATE normal_messages
                   SET status = 'discarded', processed_at = ?
                   WHERE status = 'pending'""",
                (processed_at,),
            ).rowcount
        print(f"[NORMAL DB] discarded={changed} reason={reason}")
    except Exception as exc:
        print(f"[NORMAL DB ERROR] discard failed: {type(exc).__name__}: {exc}")


def persist_category_stats(run_at, snapshots, category_results, channels):
    if not stats_db_ready:
        return
    by_id = {item["id"]: item for item in snapshots}
    received_by_channel = {
        channel: sum(1 for item in snapshots if item["channel"] == channel)
        for channel in channels
    }
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            for category_key, category_data in category_results.items():
                selected_ids = set(category_data.get("selected_ids", []))
                for channel in channels:
                    valid = sum(
                        1 for message_id in selected_ids
                        if message_id in by_id and by_id[message_id]["channel"] == channel
                    )
                    connection.execute(
                        """INSERT OR REPLACE INTO hourly_category_stats
                           (run_at, category, channel, received, valid)
                           VALUES (?, ?, ?, ?, ?)""",
                        (run_at, category_key, channel, received_by_channel[channel], valid),
                    )
                for message_id in selected_ids:
                    item = by_id.get(message_id)
                    if item:
                        connection.execute(
                            """INSERT OR REPLACE INTO hourly_classifications
                               (run_at, message_id, channel, category, preview)
                               VALUES (?, ?, ?, ?, ?)""",
                            (run_at, message_id, item["channel"], category_key, item["text"][:300]),
                        )
    except Exception as exc:
        print(f"[STATS DB ERROR] write failed: {type(exc).__name__}: {exc}")
