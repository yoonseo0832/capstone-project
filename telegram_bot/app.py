"""텔레그램 봇 본체.

- 일반 메시지   → 일정 비서(assistant)가 답변 (Gemini)
- /today /week → DB 에서 바로 (LLM 없음, 즉시)
- /sync        → LMS 지금 수집
- 주기 확인(기본 5분): 아침 브리핑(하루 1번), 마감 N시간 전 알림
- TELEGRAM_CHAT_ID 가 아닌 대화는 전부 무시 (혼자 쓰는 봇)

비서·LMS 수집은 동기(blocking) 코드라 asyncio.to_thread 로 돌려서 봇이 멈추지 않게 한다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import get_key
from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from assistant.bot import ScheduleAssistant
from crawler.crawler_db_connect import import_schedules_from_crawler_db_to_common_db
from lms.config import PROJECT_ROOT, Settings, load_settings
from lms.sync import init_all, sync_lms
from my_scrapper import WebScrapper
from telegram_bot import messages, state
from telegram_bot.config import BotSettings, load_bot_settings

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)  # 링크 미리보기 카드 끄기 (목록이 길어짐)
COMMANDS = [
    BotCommand("today", "오늘 마감"),
    BotCommand("week", "이번 주 마감"),
    BotCommand("notice", "최근 공지"),
    BotCommand("sync", "LMS 지금 새로고침"),
    BotCommand("help", "사용법"),
]
HELP = ("📅 일정 비서예요. 그냥 말로 물어보세요.\n"
        "예) 이번 주 마감 뭐 있어? / 중간고사 언제야? / Week 04 Lab 끝냈어 / 10월 20일 3시 캡스톤 회의 추가해줘\n\n"
        "/today 오늘 마감\n/week 이번 주 마감\n/notice 최근 공지\n/sync LMS 지금 새로고침\n\n"
        "아침 브리핑과 마감 알림은 자동으로 보내드려요. 새 공지도 아침에 함께 알려드려요.")


class ScheduleBot:
    def __init__(self, settings: Settings, bot_settings: BotSettings, assistant: ScheduleAssistant | None = None):
        self.s = settings
        self.b = bot_settings
        self.assistant = assistant or ScheduleAssistant(settings)
        init_all(settings)
        state.init_db(bot_settings.state_db)

    # ------------------------------------------------------------ 보내기

    async def send(self, bot, text: str) -> None:
        for chunk in messages.split_message(text):
            await bot.send_message(self.b.chat_id, chunk, link_preview_options=NO_PREVIEW)

    async def reply(self, update: Update, text: str) -> None:
        for chunk in messages.split_message(text):
            await update.effective_message.reply_text(chunk, link_preview_options=NO_PREVIEW)

    # ------------------------------------------------------------ 명령어 / 메시지

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.reply(update, HELP)

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.reply(update, messages.today_text(self.s.schedule_db, datetime.now()))

    async def cmd_week(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.reply(update, messages.week_text(self.s.schedule_db, datetime.now()))

    async def cmd_notice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.reply(update, messages.notices_text(self.s.lms_raw_db))

    async def cmd_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_chat.send_action(ChatAction.TYPING)
        try:
            result = await asyncio.to_thread(sync_lms, self.s)
            await self.reply(update, f"🔄 {result}")
        except Exception as e:
            log.exception("수동 수집 실패")
            await self.reply(update, f"⚠️ LMS 수집 실패: {e}")

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_chat.send_action(ChatAction.TYPING)
        question = update.effective_message.text
        answer = await asyncio.to_thread(lambda: "".join(self.assistant.ask(question)))
        await self.reply(update, answer.strip() or "(답변이 비어 있어요. 다시 물어봐 주세요)")

    async def on_stranger(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        log.warning("허용되지 않은 대화 무시: chat_id=%s", chat.id if chat else None)

    # ------------------------------------------------------------ 주기 작업

    async def scheduled_crawl(self, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """매일 정해진 시각(오전 6시)에 호출되어 크롤링 및 공유 DB 반영을 수행"""
        log.info("⏰ 예약된 크롤링 작업 시작...")
        env_path = PROJECT_ROOT / ".env.crawler"
        api_key = get_key(env_path, "API_KEY")
        scrapper = WebScrapper(api_key=api_key)
        try:
            # 봇이 멈추지 않도록 백그라운드 스레드에서 크롤링 수행
            await asyncio.to_thread(asyncio.run, scrapper.run())
            count = await asyncio.to_thread(
                import_schedules_from_crawler_db_to_common_db,
                scrapper,
                str(self.s.schedule_db),
            )
            log.info("✅ 예약 크롤링 완료: %d건 일정 반영", count)
            return True
        except Exception:
            log.exception("⚠️ 예약 크롤링 중 오류 발생")
            return False
        

    async def tick(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """BOT_CHECK_MINUTES 마다: 브리핑 → 마감 알림. 한 작업이 실패해도 다른 작업은 계속."""
        for job in (self.maybe_briefing, self.send_reminders):
            try:
                await job(context.bot)
            except Exception:
                log.exception("%s 실패", job.__name__)

    async def maybe_briefing(self, bot, now: datetime | None = None) -> bool:
        """오늘 브리핑을 아직 안 보냈고 브리핑 시각이 지났으면 보낸다.
        PC 를 늦게 켜도 켜진 직후에 하루 1번 보내진다."""
        now = now or datetime.now()
        if now.strftime("%H:%M") < self.b.briefing_time:
            return False
        today = now.strftime("%Y-%m-%d")
        if state.get(self.b.state_db, "last_briefing") == today:
            return False
        note = None
        try:  # 최신 상태로 보내기 위해 먼저 수집
            await asyncio.to_thread(sync_lms, self.s)
        except Exception as e:
            log.exception("브리핑 전 수집 실패")
            note = f"⚠️ LMS 수집에 실패해서 마지막으로 저장된 일정 기준이에요 ({type(e).__name__})"
        text = messages.briefing_text(self.s.schedule_db, now, note)
        new_block = messages.notices_block(self._new_notices(), "📢 새 공지")
        if new_block:
            text += "\n\n" + new_block
        await self.send(bot, text)
        state.put(self.b.state_db, "last_briefing", today)
        return True

    def _new_notices(self) -> list[dict]:
        """지난 브리핑 이후 새로 올라온 공지. 첫 실행이면 기존 공지를 '이미 알림'으로
        기록만 하고(도배 방지) 아무것도 반환하지 않는다."""
        import sqlite3

        from lms import raw_db
        try:
            items = raw_db.list_announcements(self.s.lms_raw_db, limit=100)
        except sqlite3.OperationalError:
            return []  # 아직 수집 전이라 raw_items 테이블이 없는 경우
        announced = state.announced_notice_ids(self.b.state_db)
        if not announced:  # 첫 브리핑: 기존 공지를 전부 봤다고 치고 표시하지 않음
            state.mark_notices_announced(self.b.state_db, [it["source_id"] for it in items])
            return []
        new = [it for it in items if it["source_id"] not in announced]
        state.mark_notices_announced(self.b.state_db, [it["source_id"] for it in new])
        return new

    async def send_reminders(self, bot, now: datetime | None = None) -> int:
        now = now or datetime.now()
        due = messages.reminders_due(messages.pending(self.s.schedule_db), now, self.b.remind_hours,
                                     state.sent_keys(self.b.state_db))
        if not due:
            return 0
        # 이미 냈는데 아직 반영 안 된 과제에 알림이 가지 않도록, 보낼 게 있을 때만 한 번 수집해서 다시 확인
        try:
            await asyncio.to_thread(sync_lms, self.s)
            due = messages.reminders_due(messages.pending(self.s.schedule_db), now, self.b.remind_hours,
                                         state.sent_keys(self.b.state_db))
        except Exception:
            log.exception("알림 전 수집 실패 → 저장된 일정 기준으로 알림")
        for row, hours, mark in due:
            await self.send(bot, messages.reminder_text(row, hours, now))
            state.mark_sent(self.b.state_db, row["id"], row["due_at"], mark)
        return len(due)

    # ------------------------------------------------------------ 조립

    def build(self) -> Application:
        app = Application.builder().token(self.b.token).post_init(self._post_init).build()
        mine = filters.Chat(chat_id=self.b.chat_id)
        app.add_handler(CommandHandler(["start", "help"], self.cmd_help, filters=mine))
        app.add_handler(CommandHandler("today", self.cmd_today, filters=mine))
        app.add_handler(CommandHandler("week", self.cmd_week, filters=mine))
        app.add_handler(CommandHandler("notice", self.cmd_notice, filters=mine))
        app.add_handler(CommandHandler("sync", self.cmd_sync, filters=mine))
        app.add_handler(MessageHandler(mine & filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(MessageHandler(~mine, self.on_stranger))
        app.job_queue.run_repeating(self.tick, interval=self.b.check_minutes * 60, first=5)
        # 매일 오전 6시에 자동 크롤링 수행
        crawl_time = time.fromisoformat(self.b.crawl_time).replace(tzinfo=ZoneInfo(self.s.timezone))
        app.job_queue.run_daily(self.scheduled_crawl, time=crawl_time, name="daily_crawl")
        return app

    async def _post_init(self, app: Application) -> None:
        await app.bot.set_my_commands(COMMANDS)  # 텔레그램 입력창의 '/' 메뉴


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 폴링 요청 로그(봇 토큰이 URL 에 포함됨) 숨김
    bot = ScheduleBot(load_settings(), load_bot_settings())
    log.info("텔레그램 봇 시작 (브리핑 %s, 알림 %s시간 전, %d분마다 확인)",
             bot.b.briefing_time, bot.b.remind_hours, bot.b.check_minutes)
    # 봇이 꺼져 있던 동안 텔레그램에 쌓인 메시지는 버린다 → 재시작 직후 밀린 명령/질문이
    # 한꺼번에 재생되며 답장이 도배되는 것을 막는다. 아침 브리핑은 켜진 직후 1회만 나간다.
    bot.build().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
