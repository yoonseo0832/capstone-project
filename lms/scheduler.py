"""매일 아침 LMS 수집 + 매일 자정 완료 일정 삭제를 돌리는 상주 프로세스."""
from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from lms.config import Settings
from lms.sync import cleanup, sync_lms

log = logging.getLogger(__name__)


def _hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def _safe(job, settings: Settings):
    def run():
        try:
            job(settings)
        except Exception:
            log.exception("%s 실행 실패", job.__name__)
    run.__name__ = job.__name__
    return run


def run_scheduler(settings: Settings, run_now: bool = False) -> None:
    sched = BlockingScheduler(timezone=settings.timezone)
    sh, sm = _hm(settings.sync_time)
    ch, cm = _hm(settings.cleanup_time)
    sched.add_job(_safe(sync_lms, settings), CronTrigger(hour=sh, minute=sm), id="lms_sync",
                  misfire_grace_time=3600, coalesce=True)
    sched.add_job(_safe(cleanup, settings), CronTrigger(hour=ch, minute=cm), id="cleanup_completed",
                  misfire_grace_time=3600, coalesce=True)
    log.info("스케줄러 시작: LMS 수집 매일 %s, 완료 일정 삭제 매일 %s (%s)",
             settings.sync_time, settings.cleanup_time, settings.timezone)
    if run_now:
        _safe(sync_lms, settings)()
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("스케줄러 종료")
