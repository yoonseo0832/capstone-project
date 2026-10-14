"""봇이 보낸 알림 기록 (중복 알림 방지). 봇 전용 DB — 공유 일정 DB 는 건드리지 않는다."""
from __future__ import annotations

from pathlib import Path

from common.schedule_db import connect, now_str

SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_reminders (
    schedule_id INTEGER NOT NULL,
    due_at      TEXT    NOT NULL,   -- 마감이 바뀌면 다시 알리도록 키에 포함
    hours       INTEGER NOT NULL,   -- 몇 시간 전 알림인지
    sent_at     TEXT    NOT NULL,
    PRIMARY KEY (schedule_id, due_at, hours)
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS announced_notices (
    source_id    TEXT PRIMARY KEY,   -- raw_items.source_id (공지/게시판/게시글)
    announced_at TEXT NOT NULL
);
"""


def init_db(path: str | Path) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def sent_keys(path) -> set[tuple[int, str, int]]:
    with connect(path) as conn:
        return {(r[0], r[1], r[2]) for r in conn.execute("SELECT schedule_id, due_at, hours FROM sent_reminders")}


def mark_sent(path, schedule_id: int, due_at: str, hours_list: list[int]) -> None:
    ts = now_str()
    with connect(path) as conn:
        conn.executemany("INSERT OR IGNORE INTO sent_reminders VALUES (?, ?, ?, ?)",
                         [(schedule_id, due_at, h, ts) for h in hours_list])


def announced_notice_ids(path) -> set[str]:
    with connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT source_id FROM announced_notices")}


def mark_notices_announced(path, source_ids: list[str]) -> None:
    ts = now_str()
    with connect(path) as conn:
        conn.executemany("INSERT OR IGNORE INTO announced_notices VALUES (?, ?)",
                         [(sid, ts) for sid in source_ids])


def get(path, key: str) -> str | None:
    with connect(path) as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


def put(path, key: str, value: str) -> None:
    with connect(path) as conn:
        conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))
