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
    return BotSettings(token="x", chat_id=1, briefing_time="07:00", crawl_time="06:00",
                       remind_hours=[24, 3], check_minutes=5, state_db=tmp_path / "bot.db")


def add(settings, title, due, course="262R (세종-학부)빅데이터개론(영강)-01분반", done=False):
    sid = schedule_db.add_schedule(settings.schedule_db, title, f"[{course}] 설명", due,
                                   url=f"https://lms/{title}", source="lms", source_id=title)
    if done:
        schedule_db.set_completed(settings.schedule_db, sid)
    return sid


NOTICE_SOURCE = {"announcement": "notice:lms", "lx_board": "notice:board"}


def add_notice(settings, source_id, title, itype="announcement",
               course="262R (세종-학부)빅데이터개론(영강)-01분반"):
    """공지를 공유 DB(schedules)에 due_at=NULL, source='notice:...' 로 저장."""
    schedule_db.upsert_by_source(
        settings.schedule_db, source=NOTICE_SOURCE[itype], source_id=source_id,
        title=title, content=f"[{course}] 본문", due_at=None, url=f"https://lms/n/{source_id}")


# ---------------------------------------------------------------- 문장 생성

def test_today_and_week_text(settings):
    add(settings, "오늘과제", "2026-10-13 23:59")
    add(settings, "목요일과제", "2026-10-15 23:59")
    add(settings, "다음주과제", "2026-10-20 23:59")
    add(settings, "끝낸과제", "2026-10-13 18:00", done=True)

    today = messages.today_text(settings.schedule_db, NOW)
    assert "오늘 마감 1건" in today and "D-day 2026-10-13(화) 23:59 | 빅데이터개론 | 오늘과제" in today
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
    assert "2026-10-13(화)" in text
    assert "오늘 마감 1건" in text and "이번 주 남은 마감 1건" in text and "다음 주 1건" in text
    assert text.endswith("⚠️ 수집 실패")


def test_notices_text(settings):
    assert "없어요" in messages.notices_text(settings.schedule_db)  # 비었을 때
    add_notice(settings, "announcement:1", "중간고사 안내")
    add_notice(settings, "lxboard:2", "기말고사 안내", itype="lx_board")
    txt = messages.notices_text(settings.schedule_db)
    assert "최근 공지 2건" in txt
    assert "[빅데이터개론] 중간고사 안내" in txt
    assert "https://lms/n/lxboard:2" in txt  # Canvas 공지 + 게시판 공지 모두


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


def test_state_announced_notices(bot_settings):
    state.init_db(bot_settings.state_db)
    assert state.announced_notice_ids(bot_settings.state_db) == set()
    state.mark_notices_announced(bot_settings.state_db, ["a", "b"])
    state.mark_notices_announced(bot_settings.state_db, ["b", "c"])  # 중복은 무시
    assert state.announced_notice_ids(bot_settings.state_db) == {"a", "b", "c"}


def test_briefing_shows_only_new_notices(schedule_bot, settings):
    add(settings, "오늘과제", "2026-10-13 23:59")
    add_notice(settings, "announcement:1", "기존공지")  # 첫 브리핑 전부터 있던 공지
    tg = FakeBot()
    # 첫 브리핑: 기존 공지는 '이미 알림' 처리 → 표시 안 함(도배 방지)
    asyncio.run(schedule_bot.maybe_briefing(tg, datetime(2026, 10, 13, 10, 0)))
    assert "새 공지" not in tg.sent[0][1]
    # 다음 날 새 공지 추가 → 브리핑에 그것만 등장
    add_notice(settings, "lxboard:9", "새중간고사공지", itype="lx_board")
    asyncio.run(schedule_bot.maybe_briefing(tg, datetime(2026, 10, 14, 10, 0)))
    latest = tg.sent[1][1]
    assert "📢 새 공지" in latest and "새중간고사공지" in latest and "기존공지" not in latest


# ---------------------------------------------------------------- 크롤러 명령어 (가짜 비서·텔레그램)

class FakeThread:
    def __init__(self, alive):
        self.alive = alive

    def is_alive(self):
        return self.alive


class FakeCrawlAssistant:
    """비서의 크롤러 함수 호출만 기록한다."""
    def __init__(self):
        self.calls = []
        self.crawl_thread = None

    def run_action(self, name, args):
        self.calls.append((name, args))
        if name == "run_crawler":
            self.crawl_thread = FakeThread(alive=True)
        return f"<{name}>"


class FakeJob:
    def __init__(self, callback, name, data):
        self.callback, self.name, self.data, self.removed = callback, name, data, False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_repeating(self, callback, interval, first, name, data):
        self.jobs.append(FakeJob(callback, name, data))

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name and not j.removed]


class FakeChat:
    async def send_action(self, action):
        pass


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self):
        self.effective_chat = FakeChat()
        self.effective_message = FakeMessage()


class FakeContext:
    def __init__(self, job_queue, args=None, job=None, bot=None):
        self.job_queue, self.args, self.job, self.bot = job_queue, args or [], job, bot


@pytest.fixture
def crawl_bot(settings, bot_settings):
    import telegram_bot.app as app_mod
    return app_mod.ScheduleBot(settings, bot_settings, assistant=FakeCrawlAssistant())


def test_crawl_add_and_remove_need_url(crawl_bot):
    up = FakeUpdate()
    asyncio.run(crawl_bot.cmd_crawl_add(up, FakeContext(FakeJobQueue())))
    assert "URL을 함께 보내 주세요" in up.effective_message.replies[0] and crawl_bot.assistant.calls == []

    asyncio.run(crawl_bot.cmd_crawl_add(up, FakeContext(FakeJobQueue(), args=["https://a", "https://b"])))
    asyncio.run(crawl_bot.cmd_crawl_remove(up, FakeContext(FakeJobQueue(), args=["https://a"])))
    assert crawl_bot.assistant.calls == [("add_crawler_urls", {"urls": ["https://a", "https://b"]}),
                                         ("remove_crawler_urls", {"urls": ["https://a"]})]
    assert up.effective_message.replies[-1] == "<remove_crawler_urls>"


def test_crawl_command_notifies_when_done(crawl_bot):
    jq, up = FakeJobQueue(), FakeUpdate()
    asyncio.run(crawl_bot.cmd_crawl(up, FakeContext(jq)))
    assert up.effective_message.replies == ["<run_crawler>"]
    assert len(jq.get_jobs_by_name("crawl_watch")) == 1
    assert crawl_bot.watch_crawl(jq) is False  # 이미 확인 중이면 하나만

    job, tg = jq.jobs[0], FakeBot()
    asyncio.run(crawl_bot.check_crawl_done(FakeContext(jq, job=job, bot=tg)))
    assert tg.sent == [] and not job.removed  # 아직 진행 중

    crawl_bot.assistant.crawl_thread.alive = False
    asyncio.run(crawl_bot.check_crawl_done(FakeContext(jq, job=job, bot=tg)))
    assert job.removed and tg.sent == [(1, "<get_crawl_result>")]


def test_scheduled_crawl_imports_silently_and_skips_if_running(crawl_bot):
    jq = FakeJobQueue()
    asyncio.run(crawl_bot.scheduled_crawl(FakeContext(jq)))
    job = jq.jobs[0]
    assert job.data == {"auto_import": True, "notify": False}

    asyncio.run(crawl_bot.scheduled_crawl(FakeContext(jq)))  # 진행 중이면 다시 시작하지 않음
    assert [c[0] for c in crawl_bot.assistant.calls] == ["run_crawler"]

    crawl_bot.assistant.crawl_thread.alive = False
    tg = FakeBot()
    asyncio.run(crawl_bot.check_crawl_done(FakeContext(jq, job=job, bot=tg)))
    assert [c[0] for c in crawl_bot.assistant.calls] == ["run_crawler", "get_crawl_result", "import_from_crawler"]
    assert tg.sent == []  # 새벽 예약 크롤링은 메시지를 보내지 않음
