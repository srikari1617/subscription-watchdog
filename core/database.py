"""
core/database.py — SQLite Database Layer for Subscription Watchdog

Purpose:
    Manages the local SQLite database (subscriptions.db) that stores all
    structured subscription data, flags, and the audit trail. This is the
    single persistence layer for the entire system.

Design decisions:
    - Local SQLite only — no external database dependency, so the project is
      trivially reproducible from the GitHub repo (PRD Section 7, Section 11).
    - Three tables: subscriptions, flags, audit_log (PRD Section 7).
    - Raw email content is NEVER stored — only structured fields from the
      Subscription dataclass are persisted (PRD Section 9, data minimization).
    - Every flag, draft, and approval decision is logged to audit_log
      (PRD Section 9, audit trail requirement).

PRD References:
    - Section 7 (Data Model — storage specification)
    - Section 9 (Security — audit trail, data minimization)
    - Section 10, Requirements 3, 4, 9
"""

import sqlite3
import json
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from core.models import Subscription, Flag, Draft

# Default database path — lives alongside the project root
DEFAULT_DB_PATH = Path(__file__).parent.parent / "subscriptions.db"


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """
    Returns a SQLite connection with row_factory set to sqlite3.Row
    for dict-like access to query results.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    """
    Creates the database tables if they don't already exist.

    Tables:
        subscriptions — stores structured subscription data extracted from emails
        flags         — stores detected issues (price increases, unused, new subs)
        audit_log     — immutable record of every decision and action taken

    This function is idempotent and safe to call on every startup.
    """
    conn = get_connection(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant            TEXT NOT NULL,
                amount              REAL NOT NULL,
                currency            TEXT NOT NULL DEFAULT 'USD',
                billing_cycle       TEXT NOT NULL DEFAULT 'monthly',
                next_billing_date   TEXT,
                last_known_amount   REAL NOT NULL DEFAULT 0.0,
                still_used          INTEGER,          -- 1=True, 0=False, NULL=unknown
                source_email_id     TEXT NOT NULL DEFAULT '',
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS flags (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id     INTEGER NOT NULL,
                reason              TEXT NOT NULL,     -- 'price_increase' | 'unused' | 'new_subscription'
                severity            TEXT NOT NULL,     -- 'info' | 'review' | 'action_recommended'
                reasoning_trace     TEXT NOT NULL DEFAULT '',
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (subscription_id) REFERENCES subscriptions(id)
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type         TEXT NOT NULL,     -- 'flag' | 'draft' | 'approval' | 'action'
                entity_id           INTEGER,
                action              TEXT NOT NULL,     -- human-readable description of what happened
                details             TEXT,              -- JSON blob for additional context
                timestamp           TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Subscription CRUD
# ---------------------------------------------------------------------------

def upsert_subscription(sub: Subscription, db_path: Path = DEFAULT_DB_PATH) -> int:
    """
    Inserts a new subscription or updates an existing one (matched by merchant).

    When updating, the previous amount is preserved as last_known_amount so the
    Comparator Agent can detect price changes on the next scan.

    Returns the row id of the inserted/updated subscription.
    """
    conn = get_connection(db_path)
    try:
        # Check if this merchant already exists
        existing = conn.execute(
            "SELECT id, amount FROM subscriptions WHERE merchant = ?",
            (sub.merchant,)
        ).fetchone()

        if existing:
            # Update: preserve old amount as last_known_amount for diff detection
            conn.execute("""
                UPDATE subscriptions
                SET amount = ?,
                    currency = ?,
                    billing_cycle = ?,
                    next_billing_date = ?,
                    last_known_amount = ?,
                    still_used = ?,
                    source_email_id = ?,
                    updated_at = datetime('now')
                WHERE id = ?
            """, (
                sub.amount,
                sub.currency,
                sub.billing_cycle,
                sub.next_billing_date.isoformat() if sub.next_billing_date else None,
                existing["amount"],  # previous amount becomes last_known_amount
                _bool_to_int(sub.still_used),
                sub.source_email_id,
                existing["id"],
            ))
            conn.commit()
            return existing["id"]
        else:
            # Insert new subscription
            cursor = conn.execute("""
                INSERT INTO subscriptions
                    (merchant, amount, currency, billing_cycle, next_billing_date,
                     last_known_amount, still_used, source_email_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sub.merchant,
                sub.amount,
                sub.currency,
                sub.billing_cycle,
                sub.next_billing_date.isoformat() if sub.next_billing_date else None,
                sub.last_known_amount,
                _bool_to_int(sub.still_used),
                sub.source_email_id,
            ))
            conn.commit()
            return cursor.lastrowid
    finally:
        conn.close()


def get_all_subscriptions(db_path: Path = DEFAULT_DB_PATH) -> List[Subscription]:
    """
    Returns all subscriptions currently stored in the database.
    Used by the Comparator Agent to diff against new scan results.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT * FROM subscriptions").fetchall()
        return [_row_to_subscription(row) for row in rows]
    finally:
        conn.close()


def get_subscription_by_merchant(
    merchant: str, db_path: Path = DEFAULT_DB_PATH
) -> Optional[Subscription]:
    """
    Looks up a subscription by merchant name.
    Returns None if not found.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE merchant = ?", (merchant,)
        ).fetchone()
        return _row_to_subscription(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Flag persistence
# ---------------------------------------------------------------------------

def insert_flag(flag: Flag, db_path: Path = DEFAULT_DB_PATH) -> int:
    """
    Persists a Flag to the database and writes an audit_log entry.

    The flag's subscription must already be persisted (have an id).
    Returns the new flag row id.
    """
    conn = get_connection(db_path)
    try:
        sub_id = flag.subscription.id
        if sub_id is None:
            raise ValueError("Flag's subscription must be persisted before the flag.")

        cursor = conn.execute("""
            INSERT INTO flags (subscription_id, reason, severity, reasoning_trace)
            VALUES (?, ?, ?, ?)
        """, (sub_id, flag.reason, flag.severity, flag.reasoning_trace))

        flag_id = cursor.lastrowid

        # Audit trail: log every flag (PRD Section 9)
        _write_audit(conn, "flag", flag_id, f"Flag created: {flag.reason}", {
            "merchant": flag.subscription.merchant,
            "severity": flag.severity,
            "reasoning": flag.reasoning_trace,
        })

        conn.commit()
        return flag_id
    finally:
        conn.close()


def get_pending_flags(db_path: Path = DEFAULT_DB_PATH) -> List[Flag]:
    """
    Returns all flags that have not yet been processed into drafts.
    Used by the Decision Agent to evaluate which flags to escalate.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT f.*, s.merchant, s.amount, s.currency, s.billing_cycle,
                   s.next_billing_date, s.last_known_amount, s.still_used,
                   s.source_email_id, s.id as sub_id
            FROM flags f
            JOIN subscriptions s ON f.subscription_id = s.id
            ORDER BY f.created_at DESC
        """).fetchall()

        flags = []
        for row in rows:
            sub = _row_to_subscription_from_join(row)
            flag = Flag(
                subscription=sub,
                reason=row["reason"],
                severity=row["severity"],
                reasoning_trace=row["reasoning_trace"],
                created_at=datetime.fromisoformat(row["created_at"]),
                id=row["id"],
            )
            flags.append(flag)
        return flags
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def log_audit(
    entity_type: str,
    entity_id: Optional[int],
    action: str,
    details: Optional[dict] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """
    Writes an entry to the audit_log table.

    This is a public interface for the Human Approval Gate and Action Agent
    to record approval decisions and executed actions.

    PRD Section 9: "Every Flag and Draft, with reasoning trace and timestamp,
    is written to an audit_log table."
    """
    conn = get_connection(db_path)
    try:
        _write_audit(conn, entity_type, entity_id, action, details)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_audit(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: Optional[int],
    action: str,
    details: Optional[dict] = None,
) -> None:
    """Internal: writes an audit row within an existing connection/transaction."""
    conn.execute("""
        INSERT INTO audit_log (entity_type, entity_id, action, details)
        VALUES (?, ?, ?, ?)
    """, (
        entity_type,
        entity_id,
        action,
        json.dumps(details) if details else None,
    ))


def _bool_to_int(val: Optional[bool]) -> Optional[int]:
    """Converts Python bool/None to SQLite integer representation."""
    if val is None:
        return None
    return 1 if val else 0


def _int_to_bool(val: Optional[int]) -> Optional[bool]:
    """Converts SQLite integer to Python bool/None."""
    if val is None:
        return None
    return bool(val)


def _row_to_subscription(row: sqlite3.Row) -> Subscription:
    """Converts a database row from the subscriptions table to a Subscription dataclass."""
    return Subscription(
        id=row["id"],
        merchant=row["merchant"],
        amount=row["amount"],
        currency=row["currency"],
        billing_cycle=row["billing_cycle"],
        next_billing_date=(
            date.fromisoformat(row["next_billing_date"])
            if row["next_billing_date"] else None
        ),
        last_known_amount=row["last_known_amount"],
        still_used=_int_to_bool(row["still_used"]),
        source_email_id=row["source_email_id"],
    )


def _row_to_subscription_from_join(row: sqlite3.Row) -> Subscription:
    """
    Converts a joined query row (flags + subscriptions) to a Subscription dataclass.
    Uses 'sub_id' alias for the subscription primary key.
    """
    return Subscription(
        id=row["sub_id"],
        merchant=row["merchant"],
        amount=row["amount"],
        currency=row["currency"],
        billing_cycle=row["billing_cycle"],
        next_billing_date=(
            date.fromisoformat(row["next_billing_date"])
            if row["next_billing_date"] else None
        ),
        last_known_amount=row["last_known_amount"],
        still_used=_int_to_bool(row["still_used"]),
        source_email_id=row["source_email_id"],
    )
