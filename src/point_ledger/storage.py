"""记分资格账本的 SQLite 模式与事务辅助。

所有余额都由 point_ledger_events 重算得到：撤销、申诉变更、跨周期结转
只追加新事件，绝不更新或删除旧行。point_ledger_measures 保存按业务时钟
物化过的措施，以稳定 dedupe_key 为主键，保证重算/重放不会重复执行。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ledger_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('officer','reviewer','auditor','admin')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS point_drivers (
    driver_id TEXT PRIMARY KEY,
    license_issued_on TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS point_rule_versions (
    version TEXT PRIMARY KEY,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    thresholds_json TEXT NOT NULL,
    suspend_days INTEGER,
    created_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE IF NOT EXISTS point_ledger_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id TEXT NOT NULL REFERENCES point_drivers(driver_id),
    kind TEXT NOT NULL CHECK(kind IN ('penalty_added','penalty_removed','points_cleared','cycle_reset')),
    points INTEGER NOT NULL CHECK(points >= 0 AND points <= 12),
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    cycle_start TEXT NOT NULL,
    rule_version TEXT,
    penalty_id TEXT,
    violation_id TEXT,
    clear_kind TEXT CHECK(clear_kind IS NULL OR clear_kind IN ('period_study','full_study')),
    reason TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    ref_event_seq INTEGER REFERENCES point_ledger_events(seq)
);

CREATE INDEX IF NOT EXISTS idx_point_events_driver
ON point_ledger_events(driver_id, seq);

CREATE TABLE IF NOT EXISTS point_measures (
    dedupe_key TEXT PRIMARY KEY,
    driver_id TEXT NOT NULL REFERENCES point_drivers(driver_id),
    measure TEXT NOT NULL CHECK(measure IN ('study','full_study','suspend','restore')),
    cycle_start TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    trigger_event_seq INTEGER,
    status TEXT NOT NULL CHECK(status IN ('notified','active','released','rescinded')),
    total_points INTEGER,
    due_at TEXT NOT NULL,
    release_at TEXT,
    executed_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_point_measures_driver
ON point_measures(driver_id, due_at);

CREATE TABLE IF NOT EXISTS point_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS ledger_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_audit_entity
ON ledger_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 会在工作线程中复用同一连接；写事务由
    # BEGIN IMMEDIATE + busy_timeout 串行化，因此可关闭同线程检查。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10,
                                 check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
