"""DB 의 일정을 LLM 프롬프트에 넣을 짧은 텍스트로 만든다.

일정이 수십 건 수준이라 도구 호출로 조회시키는 대신 매 질문마다 통째로 넣는다
→ LLM 호출 1번으로 답변 가능 (빠름).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

from common import schedule_db

WEEKDAYS = "월화수목금토일"
MAX_ROWS = 80
MAX_DESC = 100
MAX_NOTICES = 40       # 프롬프트에 넣을 공지 최대 건수 (Canvas 공지 + Board 게시판 공지 합산)
MAX_NOTICE_DESC = 500  # 공지 본문은 시험 날짜·장소 등 세부가 중요해 과제보다 길게


def split_course(content: str | None) -> tuple[str | None, str]:
    """'[과목명] 설명' → ('과목명', '설명'). 과목명 안에 [] 가 중첩될 수 있음."""
    if not content or not content.startswith("["):
        return None, content or ""
    depth = 0
    for i, ch in enumerate(content):
        depth += {"[": 1, "]": -1}.get(ch, 0)
        if depth == 0:
            return content[1:i], content[i + 1:].strip()
    return None, content


def short_course(name: str) -> str:
    """'262R (세종-학부)빅데이터개론(영강)(...)-01분반' → '빅데이터개론'."""
    s = re.sub(r"\[[^\]]*\]\s*", "", name)               # [학생] [한국어]
    s = re.sub(r"^\d+\w*\s*\([^)]*\)\s*", "", s)          # 262R (세종-학부)
    s = re.split(r"\(|\s-\s|-\d+분반", s)[0]               # (영강)..., - 01분반
    s = re.sub(r"^\d{4}\s+", "", s).strip()                # 2026 ...
    return s or name


def parse_due(due_at: str | None) -> datetime | None:
    """'2026-09-09T09:00:00', '2026-09-09 09:00:00', '2026-09-09', '2026-09-09 09:00' 등 처리"""
    if not due_at or not str(due_at).strip():
        return None
    s = str(due_at).strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s, schedule_db.TIME_FMT)
        except ValueError:
            return None


def d_day(due_at: str | None, now: datetime) -> str:
    due = parse_due(due_at)
    if not due:
        return "마감없음"
    days = (due.date() - now.date()).days
    if due < now:
        return "마감지남"
    return "D-day" if days == 0 else f"D-{days}"


def due_label(due_at: str | None) -> str:
    """'2026-10-07 23:59' → '2026-10-07(수) 23:59'. 연도를 넣어 LLM 이 연도를 추측(내년으로 착각)하지
    않게 한다. 크롤러 등 다양한 포맷(ISO·날짜만)도 parse_due 로 견딘다."""
    due = parse_due(due_at)
    if not due:
        return "마감없음"
    s = str(due_at).strip()
    # 시간 정보가 포함되어 있는지 확인 (T가 있거나 공백 뒤에 콜론이 있는 경우)
    if "T" in s or (" " in s and ":" in s):
        return f"{due:%Y-%m-%d}({WEEKDAYS[due.weekday()]}) {due:%H:%M}"
    return f"{due:%Y-%m-%d}({WEEKDAYS[due.weekday()]})"


def week_tag(due_at: str | None, now: datetime) -> str:
    due = parse_due(due_at)
    if not due:
        return "-"
    if due < now:
        return "지남"
    week_end = now.date() + timedelta(days=6 - now.weekday())  # 이번 주 일요일
    if due.date() <= week_end:
        return "이번주"
    if due.date() <= week_end + timedelta(days=7):
        return "다음주"
    return "이후"


def build_context(db_path, now: datetime | None = None) -> str:
    """작은 모델이 날짜 계산을 자주 틀리므로 요일·D-day·주 구분을 전부 미리 계산해서 넣는다.

    공지(source='notice:...')는 마감이 없어 [일정] 목록에서 빼고, 아래 [공지] 섹션으로 따로 붙인다
    (시험·공지 질문 답변용). 일정·공지 모두 공유 DB(schedule.db) 한 곳에서 읽는다.
    """
    now = now or datetime.now()
    rows = [r for r in schedule_db.list_schedules(db_path, include_completed=True)
            if not str(r["source"]).startswith(schedule_db.NOTICE_PREFIX)][:MAX_ROWS]
    lines = [f"현재 시각: {now:%Y-%m-%d %H:%M} ({WEEKDAYS[now.weekday()]})",
             f"일정 {len(rows)}건 (마감 순). 형식: id | 마감 | D-day | 주 | 상태 | 과목 | 제목 | 링크 | 설명"]
    for r in rows:
        course, desc = split_course(r["content"])
        desc = re.sub(r"\s+", " ", desc)[:MAX_DESC]
        lines.append(" | ".join([
            str(r["id"]), due_label(r["due_at"]), d_day(r["due_at"], now), week_tag(r["due_at"], now),
            "완료" if r["is_completed"] else "미완료",
            short_course(course) if course else ("직접추가" if r["source"] == "user" else r["source"]),
            r["title"], r["url"] or "-", desc or "-",
        ]))
    if not rows:
        lines.append("(등록된 일정 없음)")
    lines += notice_lines(db_path)
    return "\n".join(lines)


def notice_lines(db_path) -> list[str]:
    """공유 DB의 공지를 '[공지]' 섹션 텍스트로. 마감이 없으므로 과목·제목·본문·링크만 넣는다.
    과목명은 일정과 같은 규칙으로 content 의 '[과목명] 본문' 에서 꺼낸다."""
    try:
        notices = schedule_db.list_notices(db_path, limit=MAX_NOTICES)
    except sqlite3.OperationalError:
        return []  # 아직 수집 전이라 schedules 테이블이 없는 경우
    if not notices:
        return []
    out = ["", f"[공지] 게시판/공지사항 {len(notices)}건. 형식: 과목 | 제목 | 내용 | 링크"]
    for n in notices:
        course, body = split_course(n["content"])
        desc = re.sub(r"\s+", " ", body or "")[:MAX_NOTICE_DESC]
        out.append(" | ".join([
            short_course(course) if course else "-",
            n["title"], desc or "-", n["url"] or "-",
        ]))
    return out
