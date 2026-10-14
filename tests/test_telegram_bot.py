import asyncio
from datetime import datetime

import pytest

from common import schedule_db
from lms.config import Settings
from telegram_bot import messages, state
from telegram_bot.config import BotSettings

NOW = datetime(2026, 10, 13, 9, 0)  # 화요일 09:00


@pytest.fixture
def settings(tmp_path):
    s = Settings(canvas_api_url="https://lms.example", canvas_api_key=None,
                 schedule_db=tmp_path / "s.db", lms_raw_db=tmp_path / "r.db", timezone="Asia/Seoul",
                 sync_time="07:00", cleanup_time="00:00", include_no_due=False, skip_past_due=True)
    schedule_db.init_db(s.schedule_db)
    return s


@pytest.fixture
def bot_settings(tmp_path):
    return BotSettings(token="x", chat_id=1, briefing_time="07:00", remind_hours=[24, 3],
                       check_minutes=5, state_db=tmp_path / "bot.db")


def add(settings, title, due, course="262R (세종-학부)빅데이터개론(영강)-01분반", done=False):
    sid = schedule_db.add_schedule(settings.schedule_db, title, f"[{course}] 설명", due,
                                   url=f"https://lms/{title}", source="lms", source_id=title)
    if done:
        schedule_db.set_completed(settings.schedule_db, sid)
    return sid


# ---------------------------------------------------------------- 문장 생성

def test_today_and_week_text(settings):
    add(settings, "오늘과제", "2026-10-13 23:59")
    add(settings, "목요일과제", "2026-10-15 23:59")
    add(settings, "다음주과제", "2026-10-20 23:59")
    add(settings, "끝낸과제", "2026-10-13 18:00", done=True)

    today = messages.today_text(settings.schedule_db, NOW)
    assert "오늘 마감 1건" in today and "D-day 10/13(화) 23:59 | 빅데이터개론 | 오늘과제" in today
    assert "https://lms/오늘과제" in today and "끝낸과제" not in today

    week = messages.week_text(settings.schedule_db, NOW)
    assert "이번 주 마감 2건" in week and "목요일과제" in week and "다음주과제" not in week


def test_empty_texts(settings):
    assert "없어요" in messages.today_text(settings.schedule_db, NOW)
    assert "없어요" in messages.week_text(settings.schedule_db, NOW)


def test_briefing_text(settings):
    add(settings, "오늘과제", "2026-10-13 23:59")
    add(settings, "목요일과제", "2026-10-15 23:59")
    add(settings, "다음주과제", "2026-10-20 23:59")
    text = messages.briefing_text(settings.schedule_db, NOW, "⚠️ 수집 실패")
    assert "10/13(화)" in text
    assert "오늘 마감 1건" in text and "이번 주 남은 마감 1건" in text and "다음 주 1건" in text
    assert text.endswith("⚠️ 수집 실패")


def test_split_message():
    text = "\n".join(f"줄{i:03d}" for i in range(300))
    chunks = messages.split_message(text, limit=100)
    assert all(len(c) <= 100 for c in chunks) and "\n".join(chunks) == text


# ---------------------------------------------------------------- 알림 선택

def row(id_, due):
    return {"id": id_, "due_at": due}


def test_reminders_due_thresholds():
    rows = [row(1, "2026-10-14 08:00"),   # 23시간 남음 → 24h
            row(2, "2026-10-13 11:00"),   # 2시간 남음 → 3h (24h 는 건너뜀)
            row(3, "2026-10-16 23:59"),   # 아직 멀음
            row(4, "2026-10-13 08:00")]   # 이미 지남
    due = messages.reminders_due(rows, NOW, [24, 3], set())
    assert [(r["id"], h, mark) for r, h, mark in due] == [(1, 24, [24]), (2, 3, [24, 3])]


def test_reminders_not_repeated_but_due_change_resets():
    rows = [row(1, "2026-10-14 08:00")]
    assert messages.reminders_due(rows, NOW, [24, 3], {(1, "2026-10-14 08:00", 24)}) == []
    # 마감이 바뀌면 다시 알림
    moved = [row(1, "2026-10-14 06:00")]
    assert len(messages.reminders_due(moved, NOW, [24, 3], {(1, "2026-10-14 08:00", 24)})) == 1


# ---------------------------------------------------------------- 브리핑 / 알림 전송 (가짜 텔레그램)

class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


@pytest.fixture
def schedule_bot(settings, bot_settings, monkeypatch):
    import telegram_bot.app as app_mod
    calls = []
    monkeypatch.setattr(app_mod, "sync_lms", lambda s: calls.append("sync") or "ok")
    bot = app_mod.ScheduleBot(settings, bot_settings, assistant=object())
    bot.sync_calls = calls
    return bot


def test_briefing_once_per_day_and_not_before_time(schedule_bot, settings):
    add(settings, "오늘과제", "2026-10-13 23:59")
    tg = FakeBot()
    assert asyncio.run(schedule_bot.maybe_briefing(tg, datetime(2026, 10, 13, 6, 30))) is False
    assert asyncio.run(schedule_bot.maybe_briefing(tg, datetime(2026, 10, 13, 10, 0))) is True  # 늦게 켜도 보냄
    assert asyncio.run(schedule_bot.maybe_briefing(tg, datetime(2026, 10, 13, 10, 5))) is False  # 하루 1번
    assert len(tg.sent) == 1 and "오늘과제" in tg.sent[0][1] and schedule_bot.sync_calls == ["sync"]


def test_send_reminders_once(schedule_bot, settings):
    add(settings, "곧마감", "2026-10-13 11:00")
    add(settings, "낸과제", "2026-10-13 11:00", done=True)
    tg = FakeBot()
    assert asyncio.run(schedule_bot.send_reminders(tg, NOW)) == 1
    assert "곧마감" in tg.sent[0][1] and "2시간 0분" in tg.sent[0][1]
    assert asyncio.run(schedule_bot.send_reminders(tg, NOW)) == 0  # 중복 안 보냄
    assert schedule_bot.sync_calls == ["sync"]                      # 보낼 게 없으면 수집도 안 함


def test_reminder_skipped_if_submitted_during_check(schedule_bot, settings, monkeypatch):
    """알림 직전 수집에서 제출이 확인되면 알림을 보내지 않음."""
    import telegram_bot.app as app_mod
    sid = add(settings, "방금낸과제", "2026-10-13 11:00")
    monkeypatch.setattr(app_mod, "sync_lms", lambda s: schedule_db.set_completed(s.schedule_db, sid))
    tg = FakeBot()
    assert asyncio.run(schedule_bot.send_reminders(tg, NOW)) == 0 and tg.sent == []


def test_state_kv(bot_settings):
    state.init_db(bot_settings.state_db)
    assert state.get(bot_settings.state_db, "k") is None
    state.put(bot_settings.state_db, "k", "1")
    state.put(bot_settings.state_db, "k", "2")
    assert state.get(bot_settings.state_db, "k") == "2"
