"""환경 변수(.env) 기반 설정."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _path(env_name: str, default: str) -> Path:
    p = Path(os.environ.get(env_name, default))
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass(frozen=True)
class Settings:
    canvas_api_url: str
    canvas_api_key: str | None
    schedule_db: Path      # 팀 공유 일정 DB
    lms_raw_db: Path       # LMS 원본 DB (개인)
    timezone: str
    sync_time: str         # 매일 LMS 수집 시각 HH:MM
    cleanup_time: str      # 매일 완료 일정 삭제 시각 HH:MM
    include_no_due: bool   # 마감일 없는 과제도 일정에 올릴지
    skip_past_due: bool    # 이미 마감 지난 과제는 일정에 올리지 않을지


def _bool(env_name: str, default: bool) -> bool:
    return os.environ.get(env_name, str(default)).strip().lower() in ("1", "true", "yes", "y")


def load_settings() -> Settings:
    return Settings(
        canvas_api_url=os.environ.get("CANVAS_API_URL", "https://mylms.korea.ac.kr").rstrip("/"),
        canvas_api_key=os.environ.get("CANVAS_API_KEY") or None,
        schedule_db=_path("SCHEDULE_DB_PATH", "workspace/data/schedule.db"),
        lms_raw_db=_path("LMS_RAW_DB_PATH", "workspace/data/lms_raw.db"),
        timezone=os.environ.get("TZ_NAME", "Asia/Seoul"),
        sync_time=os.environ.get("LMS_SYNC_TIME", "07:00"),
        cleanup_time=os.environ.get("CLEANUP_TIME", "00:00"),
        include_no_due=_bool("LMS_INCLUDE_NO_DUE", False),
        skip_past_due=_bool("LMS_SKIP_PAST_DUE", True),
    )
