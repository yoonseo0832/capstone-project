"""공유 일정(편집) DB — 팀 전체가 같이 쓰는 유일한 테이블.

LMS 수집기, 다른 사이트 크롤러, 답변용 에이전트 모두 이 모듈로만 schedules 테이블에 접근한다.
원본 데이터(본문/링크/해시)는 각자 자기 DB에 따로 둔다.

시간 문자열은 모두 로컬(Asia/Seoul) 기준 'YYYY-MM-DD HH:MM' 형식.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

TIME_FMT = "%Y-%m-%d %H:%M"

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,              -- 제목
    content      TEXT,                          -- 내용(요약/본문)
    start_at     TEXT,                          -- 일정 시작 (없으면 NULL)
    due_at       TEXT,                          -- 일정 날짜/마감 (없으면 NULL)
    url          TEXT,                          -- 원문 링크
    source       TEXT    NOT NULL DEFAULT 'user', -- 'lms', 'crawler:<사이트>', 'user' 등
    source_id    TEXT,                          -- 원본 DB 쪽 고유키 (중복 방지/동기화용)
    is_completed INTEGER NOT NULL DEFAULT 0,    -- 완료 여부
    completed_at TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules (due_at);
CREATE INDEX IF NOT EXISTS idx_schedules_completed ON schedules (is_completed);
"""

# update_schedule 로 수정 가능한 컬럼 (컬럼명은 SQL에 직접 들어가므로 화이트리스트로 제한)
EDITABLE_COLUMNS = {"title", "content", "start_at", "due_at", "url", "is_completed"}


def now_str() -> str:
    return datetime.now().strftime(TIME_FMT)


@contextmanager
def connect(path: str | Path) -> Generator[sqlite3.Connection, None, None]:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: str | Path) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------- 생성 / 조회

def add_schedule(path, title: str, content: str | None = None, due_at: str | None = None,
                 start_at: str | None = None, url: str | None = None,
                 source: str = "user", source_id: str | None = None) -> int:
    """일정 1건 추가 후 id 반환."""
    ts = now_str()
    with connect(path) as conn:
        cur = conn.execute(
            """INSERT INTO schedules
               (title, content, start_at, due_at, url, source, source_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, content, start_at, due_at, url, source, source_id, ts, ts),
        )
        return cur.lastrowid


def get_schedule(path, schedule_id: int) -> dict | None:
    with connect(path) as conn:
        return _row(conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone())


def get_by_source(path, source: str, source_id: str) -> dict | None:
    with connect(path) as conn:
        return _row(conn.execute(
            "SELECT * FROM schedules WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone())


def list_schedules(path, include_completed: bool = True, source: str | None = None,
                   due_before: str | None = None, due_after: str | None = None) -> list[dict]:
    """조건별 일정 조회. 마감일 오름차순(마감 없는 일정은 뒤로)."""
    where, params = [], []
    if not include_completed:
        where.append("is_completed = 0")
    if source is not None:
        where.append("source = ?")
        params.append(source)
    if due_before is not None:
        where.append("due_at < ?")
        params.append(due_before)
    if due_after is not None:
        where.append("due_at >= ?")
        params.append(due_after)
    sql = "SELECT * FROM schedules"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY due_at IS NULL, due_at, id"
    with connect(path) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


# ---------------------------------------------------------------- 수정

def update_schedule(path, schedule_id: int, **fields) -> dict | None:
    """지정한 컬럼만 수정. 예: update_schedule(db, 3, title="새 제목", due_at="2026-10-10 23:59")"""
    bad = set(fields) - EDITABLE_COLUMNS
    if bad:
        raise ValueError(f"수정할 수 없는 컬럼: {sorted(bad)}")
    if not fields:
        return get_schedule(path, schedule_id)
    if "is_completed" in fields:
        fields["is_completed"] = int(bool(fields["is_completed"]))
        fields["completed_at"] = now_str() if fields["is_completed"] else None
    fields["updated_at"] = now_str()
    assignments = ", ".join(f"{col} = ?" for col in fields)
    with connect(path) as conn:
        conn.execute(f"UPDATE schedules SET {assignments} WHERE id = ?", (*fields.values(), schedule_id))
    return get_schedule(path, schedule_id)


def set_completed(path, schedule_id: int, completed: bool = True) -> dict | None:
    return update_schedule(path, schedule_id, is_completed=completed)


def upsert_by_source(path, source: str, source_id: str, title: str, content: str | None = None,
                     due_at: str | None = None, start_at: str | None = None,
                     url: str | None = None, is_completed: bool | None = None) -> tuple[int, str]:
    """수집기용: (source, source_id) 기준으로 없으면 추가, 있으면 내용 갱신.

    is_completed=None 이면 완료 여부는 건드리지 않는다(사용자가 직접 체크한 값 보존).
    반환: (id, 'inserted' | 'updated')
    """
    existing = get_by_source(path, source, source_id)
    if existing is None:
        new_id = add_schedule(path, title, content, due_at, start_at, url, source, source_id)
        if is_completed:
            set_completed(path, new_id, True)
        return new_id, "inserted"
    fields = dict(title=title, content=content, due_at=due_at, start_at=start_at, url=url)
    if is_completed is not None and bool(is_completed) != bool(existing["is_completed"]):
        fields["is_completed"] = is_completed
    update_schedule(path, existing["id"], **fields)
    return existing["id"], "updated"


# ---------------------------------------------------------------- 삭제

def delete_schedule(path, schedule_id: int) -> bool:
    with connect(path) as conn:
        return conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,)).rowcount > 0


def delete_by_source(path, source: str, source_id: str) -> bool:
    with connect(path) as conn:
        return conn.execute(
            "DELETE FROM schedules WHERE source = ? AND source_id = ?", (source, source_id)
        ).rowcount > 0


def cleanup_completed(path) -> list[dict]:
    """완료된 일정 전부 삭제(매일 자정 실행용). 삭제된 행들을 반환해서
    각 수집기가 자기 원본 DB에 '완료됨' 표시를 남길 수 있게 한다(다음 수집 때 다시 생기지 않도록)."""
    with connect(path) as conn:
        return [dict(r) for r in conn.execute("DELETE FROM schedules WHERE is_completed = 1 RETURNING *")]
