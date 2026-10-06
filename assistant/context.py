"""DB 의 일정을 LLM 프롬프트에 넣을 짧은 텍스트로 만든다.

일정이 수십 건 수준이라 도구 호출로 조회시키는 대신 매 질문마다 통째로 넣는다
→ LLM 호출 1번으로 답변 가능 (빠름).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from common import schedule_db

WEEKDAYS = "월화수목금토일"
MAX_ROWS = 80
MAX_DESC = 100


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


def d_day(due_at: str | None, now: datetime) -> str:
    if not due_at:
        return "마감없음"
    due = datetime.strptime(due_at, schedule_db.TIME_FMT)
    days = (due.date() - now.date()).days
    if due < now:
        return "마감지남"
    return "D-day" if days == 0 else f"D-{days}"


def due_label(due_at: str | None) -> str:
    """'2026-10-07 23:59' → '10/07(수) 23:59'"""
    if not due_at:
        return "마감없음"
    due = datetime.strptime(due_at, schedule_db.TIME_FMT)
    return f"{due:%m/%d}({WEEKDAYS[due.weekday()]}) {due:%H:%M}"


def week_tag(due_at: str | None, now: datetime) -> str:
    if not due_at:
        return "-"
    due = datetime.strptime(due_at, schedule_db.TIME_FMT)
    if due < now:
        return "지남"
    week_end = now.date() + timedelta(days=6 - now.weekday())  # 이번 주 일요일
    if due.date() <= week_end:
        return "이번주"
    if due.date() <= week_end + timedelta(days=7):
        return "다음주"
    return "이후"


def build_context(db_path, now: datetime | None = None) -> str:
    """작은 모델이 날짜 계산을 자주 틀리므로 요일·D-day·주 구분을 전부 미리 계산해서 넣는다."""
    now = now or datetime.now()
    rows = schedule_db.list_schedules(db_path, include_completed=True)[:MAX_ROWS]
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
    return "\n".join(lines)
