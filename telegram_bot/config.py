"""텔레그램 봇 설정 (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from lms.config import PROJECT_ROOT  # .env 로드도 여기서 됨


@dataclass(frozen=True)
class BotSettings:
    token: str
    chat_id: int              # 이 chat_id 의 메시지만 처리 (혼자 쓰는 봇)
    briefing_time: str        # 아침 브리핑 시각 HH:MM
    remind_hours: list[int]   # 마감 N시간 전 알림 (예: [24, 3])
    check_minutes: int        # 브리핑/알림 확인 주기 (분)
    state_db: Path            # 보낸 알림 기록


def load_bot_settings() -> BotSettings:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError(".env 에 TELEGRAM_BOT_TOKEN 과 TELEGRAM_CHAT_ID 가 필요합니다.")
    hours = os.environ.get("BOT_REMIND_HOURS", "24,3")
    state = Path(os.environ.get("BOT_STATE_DB_PATH", "workspace/data/bot_state.db"))
    return BotSettings(
        token=token,
        chat_id=int(chat_id),
        briefing_time=os.environ.get("BOT_BRIEFING_TIME", "07:00"),
        remind_hours=sorted({int(h) for h in hours.split(",") if h.strip()}, reverse=True),
        check_minutes=int(os.environ.get("BOT_CHECK_MINUTES", "5")),
        state_db=state if state.is_absolute() else PROJECT_ROOT / state,
    )
