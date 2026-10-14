"""DB → 텔레그램 메시지 문장 (LLM 없이 코드로 생성: 빠르고 무료 한도도 안 씀).

텔레그램 라이브러리에 의존하지 않아서 테스트하기 쉽다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from assistant.context import WEEKDAYS, d_day, due_label, short_course, split_course, week_tag
from common import schedule_db
from lms import raw_db

MAX_LEN = 4000  # 텔레그램 한 메시지 최대 4096자
MAX_NOTICES = 10  # /notice 로 보여줄 최근 공지 수


def _due(row: dict) -> datetime:
    return datetime.strptime(row["due_at"], schedule_db.TIME_FMT)


def course_of(row: dict) -> str:
    course, _ = split_course(row["content"])
    if course:
        return short_course(course)
    return "직접추가" if row["source"] == "user" else row["source"]


def item_line(row: dict, now: datetime, with_link: bool = False) -> str:
    line = f"• {d_day(row['due_at'], now)} {due_label(row['due_at'])} | {course_of(row)} | {row['title']}"
    if with_link and row.get("url"):
        line += f"\n  {row['url']}"
    return line


def pending(db_path) -> list[dict]:
    """미완료 + 마감 있는 일정 (마감 순)."""
    return [r for r in schedule_db.list_schedules(db_path, include_completed=False) if r["due_at"]]


def today_text(db_path, now: datetime) -> str:
    rows = [r for r in pending(db_path) if _due(r).date() == now.date() and _due(r) >= now]
    if not rows:
        return "오늘 남은 마감은 없어요 🎉"
    return f"📌 오늘 마감 {len(rows)}건\n" + "\n".join(item_line(r, now, True) for r in rows)


def week_text(db_path, now: datetime) -> str:
    rows = [r for r in pending(db_path) if week_tag(r["due_at"], now) == "이번주"]
    if not rows:
        return "이번 주 남은 마감은 없어요 🎉"
    return f"🗓 이번 주 마감 {len(rows)}건\n" + "\n".join(item_line(r, now, True) for r in rows)


def briefing_text(db_path, now: datetime, sync_note: str | None = None) -> str:
    rows = pending(db_path)
    today = [r for r in rows if _due(r).date() == now.date() and _due(r) >= now]
    week = [r for r in rows if week_tag(r["due_at"], now) == "이번주" and r not in today]
    nxt = [r for r in rows if week_tag(r["due_at"], now) == "다음주"]
    parts = [f"☀️ 좋은 아침이에요! {now:%m/%d}({WEEKDAYS[now.weekday()]})"]
    parts.append(f"\n📌 오늘 마감 {len(today)}건" + ("".join("\n" + item_line(r, now, True) for r in today) if today else ""))
    parts.append(f"\n🗓 이번 주 남은 마감 {len(week)}건" + ("".join("\n" + item_line(r, now) for r in week) if week else ""))
    if nxt:
        parts.append(f"\n👀 다음 주 {len(nxt)}건" + "".join("\n" + item_line(r, now) for r in nxt))
    if sync_note:
        parts.append(f"\n{sync_note}")
    return "\n".join(parts)


def notice_line(item: dict) -> str:
    course = short_course(item["course_name"]) if item.get("course_name") else "-"
    line = f"• [{course}] {item['title']}"
    if item.get("link"):
        line += f"\n  {item['link']}"
    return line


def notices_block(items: list[dict], header: str) -> str:
    """공지 목록 → 메시지 블록. 비어 있으면 빈 문자열."""
    if not items:
        return ""
    return f"{header} {len(items)}건\n" + "\n".join(notice_line(it) for it in items)


def notices_text(raw_db_path, limit: int = MAX_NOTICES) -> str:
    """/notice: 최근 공지(Canvas + 게시판)를 LLM 없이 바로."""
    items = raw_db.list_announcements(raw_db_path, limit=limit)
    if not items:
        return "등록된 공지가 없어요. /sync 로 새로고침해 보세요."
    return notices_block(items, "📢 최근 공지")


def reminders_due(rows: list[dict], now: datetime, hours: list[int],
                  sent: set[tuple[int, str, int]]) -> list[tuple[dict, int, list[int]]]:
    """지금 보내야 할 알림 목록: (일정, 보낼 알림 시간, 보낸 것으로 기록할 시간들).

    늦게 켜져서 24시간 전·3시간 전 구간을 동시에 지났으면 가장 가까운 것(3시간 전) 하나만 보낸다.
    """
    out = []
    for r in rows:
        due = _due(r)
        if due <= now:
            continue
        applicable = [h for h in hours if due - now <= timedelta(hours=h)]
        if not applicable:
            continue
        closest = min(applicable)
        if (r["id"], r["due_at"], closest) in sent:
            continue
        out.append((r, closest, applicable))
    return out


def reminder_text(row: dict, hours: int, now: datetime) -> str:
    left = _due(row) - now
    h, m = divmod(int(left.total_seconds() // 60), 60)
    remain = f"{h}시간 {m}분" if h else f"{m}분"
    return (f"⏰ 마감 {remain} 남았어요 (아직 미제출)\n"
            f"{item_line(row, now, with_link=True)}")


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """줄 단위로 잘라 텔레그램 길이 제한 안으로."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # 한 줄이 너무 긴 경우
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [""]
