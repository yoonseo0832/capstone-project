"""LMS 원본 DB (개인용, 팀 공유 X).

- source_urls : 수집 대상 URL (Canvas에서 받아온 강의 URL + 사용자가 직접 추가한 URL)
- raw_items   : Canvas에서 받은 원본 내용, 추출 링크, 변경 감지용 해시
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from common.schedule_db import connect, now_str

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_urls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT    NOT NULL UNIQUE,
    kind            TEXT    NOT NULL DEFAULT 'user',  -- 'lms_course'(자동 수집) | 'user'(사용자 추가)
    label           TEXT,
    added_at        TEXT    NOT NULL,
    last_fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS raw_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT    NOT NULL UNIQUE,   -- 예: 'assignment:12345' (schedules.source_id 와 동일)
    item_type       TEXT    NOT NULL,          -- 'assignment' 등
    course_id       INTEGER,
    course_name     TEXT,
    title           TEXT    NOT NULL,
    content         TEXT,                      -- 본문(텍스트)
    content_html    TEXT,                      -- 본문 원본 HTML
    link            TEXT,                      -- 원문 링크
    extracted_links TEXT,                      -- 본문에서 추출한 링크들(JSON 배열)
    due_at          TEXT,                      -- 로컬시간 'YYYY-MM-DD HH:MM'
    submitted       INTEGER NOT NULL DEFAULT 0,
    content_hash    TEXT    NOT NULL,          -- 내용 변경 여부 확인용
    is_done         INTEGER NOT NULL DEFAULT 0,-- 완료 처리되어 일정 테이블에서 삭제됨
    fetched_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
"""


def init_db(path: str | Path) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def compute_hash(item: dict) -> str:
    """내용이 바뀌었는지 판단하는 해시. 제출 여부는 별도로 비교하므로 포함하지 않는다."""
    key = json.dumps(
        [item.get("title"), item.get("content_html"), item.get("due_at"), item.get("link")],
        ensure_ascii=False,
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- source_urls

def add_url(path, url: str, kind: str = "user", label: str | None = None) -> bool:
    """URL 저장. 이미 있으면 False."""
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO source_urls (url, kind, label, added_at) VALUES (?, ?, ?, ?)",
            (url, kind, label, now_str()),
        )
        return cur.rowcount > 0


def list_urls(path, kind: str | None = None) -> list[dict]:
    with connect(path) as conn:
        if kind is None:
            rows = conn.execute("SELECT * FROM source_urls ORDER BY id")
        else:
            rows = conn.execute("SELECT * FROM source_urls WHERE kind = ? ORDER BY id", (kind,))
        return [dict(r) for r in rows]


def remove_url(path, url: str) -> bool:
    with connect(path) as conn:
        return conn.execute("DELETE FROM source_urls WHERE url = ?", (url,)).rowcount > 0


def touch_url(path, url: str) -> None:
    with connect(path) as conn:
        conn.execute("UPDATE source_urls SET last_fetched_at = ? WHERE url = ?", (now_str(), url))


# ---------------------------------------------------------------- raw_items

def get_item(path, source_id: str) -> dict | None:
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM raw_items WHERE source_id = ?", (source_id,)).fetchone()
        return dict(row) if row else None


def list_items(path) -> list[dict]:
    with connect(path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM raw_items ORDER BY due_at IS NULL, due_at")]


def save_item(path, item: dict) -> str:
    """원본 저장(upsert). 반환: 'new' | 'changed' | 'unchanged'.
    제출 여부만 바뀐 경우는 'unchanged' 지만 submitted 컬럼은 갱신된다."""
    h = compute_hash(item)
    ts = now_str()
    values = (
        item["item_type"], item.get("course_id"), item.get("course_name"), item["title"],
        item.get("content"), item.get("content_html"), item.get("link"),
        json.dumps(item.get("extracted_links", []), ensure_ascii=False),
        item.get("due_at"), int(bool(item.get("submitted"))), h,
    )
    existing = get_item(path, item["source_id"])
    with connect(path) as conn:
        if existing is None:
            conn.execute(
                """INSERT INTO raw_items
                   (item_type, course_id, course_name, title, content, content_html, link,
                    extracted_links, due_at, submitted, content_hash, source_id, fetched_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*values, item["source_id"], ts, ts),
            )
            return "new"
        changed = existing["content_hash"] != h
        conn.execute(
            """UPDATE raw_items SET
               item_type=?, course_id=?, course_name=?, title=?, content=?, content_html=?, link=?,
               extracted_links=?, due_at=?, submitted=?, content_hash=?, fetched_at=?,
               updated_at = CASE WHEN ? THEN ? ELSE updated_at END
               WHERE source_id=?""",
            (*values, ts, int(changed), ts, item["source_id"]),
        )
        return "changed" if changed else "unchanged"


def mark_done(path, source_ids: list[str]) -> None:
    """일정 테이블에서 완료 삭제된 항목 표시 → 다음 수집 때 다시 일정에 올리지 않음."""
    with connect(path) as conn:
        conn.executemany("UPDATE raw_items SET is_done = 1 WHERE source_id = ?", [(s,) for s in source_ids])


def delete_item(path, source_id: str) -> bool:
    with connect(path) as conn:
        return conn.execute("DELETE FROM raw_items WHERE source_id = ?", (source_id,)).rowcount > 0
