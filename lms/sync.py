"""LMS → 원본 DB → 공유 일정 DB 동기화 / 완료 일정 정리."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from common import schedule_db
from lms import raw_db
from lms.config import Settings

log = logging.getLogger(__name__)

SOURCE = "lms"
COURSE_URL_RE = re.compile(r"/courses/(\d+)")
RETRY_ATTEMPTS = 3
RETRY_WAIT = 20  # 초


@dataclass
class SyncResult:
    courses: int = 0
    fetched: int = 0
    new: int = 0
    changed: int = 0
    scheduled: int = 0   # 일정 테이블에 추가/갱신된 수
    skipped: int = 0     # 마감 지남/마감 없음/이미 완료 처리 등으로 일정에 안 올린 수
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        s = (f"강의 {self.courses}개, 과제 {self.fetched}개 수집 "
             f"(신규 {self.new}, 변경 {self.changed}) → 일정 반영 {self.scheduled}, 제외 {self.skipped}")
        if self.errors:
            s += f", 에러 {len(self.errors)}건"
        return s


def init_all(settings: Settings) -> None:
    schedule_db.init_db(settings.schedule_db)
    raw_db.init_db(settings.lms_raw_db)


def user_course_ids(settings: Settings) -> list[int]:
    """사용자가 추가한 URL 중 이 LMS 의 강의 URL 이면 강의 id 추출."""
    ids = []
    for row in raw_db.list_urls(settings.lms_raw_db, kind="user"):
        if row["url"].startswith(settings.canvas_api_url):
            m = COURSE_URL_RE.search(row["url"])
            if m:
                ids.append(int(m.group(1)))
    return ids


def _should_schedule(item: dict, settings: Settings, now: str) -> bool:
    if item["due_at"] is None:
        return settings.include_no_due
    if settings.skip_past_due and item["due_at"] < now:
        return False
    return True


def sync_items(settings: Settings, items, full: bool = False, result: SyncResult | None = None) -> SyncResult:
    """정규화된 과제 dict 들을 원본 DB에 저장하고 필요한 것만 일정 DB에 반영.

    일정 테이블은 '신규 / 내용 변경 / 제출 상태 변경' 일 때만 건드린다 → 사용자가 직접
    지운 일정이 매일 아침 되살아나지 않음. full=True 면 대상 전부 다시 반영.
    """
    result = result or SyncResult()
    now = datetime.now(ZoneInfo(settings.timezone)).strftime(schedule_db.TIME_FMT)
    for item in items:
        result.fetched += 1
        prev = raw_db.get_item(settings.lms_raw_db, item["source_id"])
        status = raw_db.save_item(settings.lms_raw_db, item)
        if status == "new":
            result.new += 1
        elif status == "changed":
            result.changed += 1
        submit_changed = prev is not None and bool(prev["submitted"]) != bool(item["submitted"])

        if prev is not None and prev["is_done"]:
            result.skipped += 1
            continue
        # 마감 지남/마감 없음 필터는 '새로 올릴지' 판단에만 쓴다. 이미 일정에 있는 과제는
        # 마감 지난 뒤(예: 자정 확인)에도 제출 → 완료 처리가 반영돼야 한다.
        in_schedule = schedule_db.get_by_source(settings.schedule_db, SOURCE, item["source_id"]) is not None
        if not in_schedule and not _should_schedule(item, settings, now):
            result.skipped += 1
            continue
        if not (full or status != "unchanged" or submit_changed):
            continue

        content = f"[{item['course_name']}] {item['content']}".strip() if item["content"] else f"[{item['course_name']}]"
        schedule_db.upsert_by_source(
            settings.schedule_db, SOURCE, item["source_id"],
            title=item["title"], content=content, due_at=item["due_at"],
            start_at=item.get("start_at"), url=item["link"],
            # 제출됨 → 완료 처리. 미제출이면 사용자가 수동 체크한 상태를 덮어쓰지 않도록 None
            is_completed=True if item["submitted"] else None,
        )
        result.scheduled += 1
    return result


def sync_lms(settings: Settings, full: bool = False, client=None) -> SyncResult:
    """Canvas 에서 과제를 받아와 동기화 (매일 아침 실행)."""
    if client is None:
        if not settings.canvas_api_key:
            raise RuntimeError(".env 에 CANVAS_API_KEY 가 없습니다.")
        from lms.canvas_client import CanvasClient
        client = CanvasClient(settings.canvas_api_url, settings.canvas_api_key, settings.timezone)

    init_all(settings)
    result = SyncResult()
    courses = _retry(lambda: client.get_courses(user_course_ids(settings)), "강의 목록")
    for course in courses:
        result.courses += 1
        url = client.course_url(course["id"])
        raw_db.add_url(settings.lms_raw_db, url, kind="lms_course", label=course["name"])
        try:
            # 강의 단위로 재시도 (같은 과제를 다시 저장해도 해시가 같아서 중복 반영 안 됨)
            _retry(lambda: sync_items(settings, client.iter_assignments(course), full=full, result=result),
                   f"강의 {course['name']}")
            raw_db.touch_url(settings.lms_raw_db, url)
        except Exception as e:  # 재시도해도 실패한 강의는 건너뛰고 나머지는 계속
            log.exception("강의 %s(%s) 수집 실패", course["name"], course["id"])
            result.errors.append(f"{course['name']}: {e}")
    log.info("LMS 동기화 완료: %s", result)
    return result


def _retry(fn, what: str, attempts: int = RETRY_ATTEMPTS):
    """네트워크 오류(타임아웃 등)는 RETRY_WAIT 초 쉬고 다시 시도."""
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            if i == attempts:
                raise
            log.warning("%s 실패 (%d/%d): %s → %d초 후 재시도", what, i, attempts, e, RETRY_WAIT)
            time.sleep(RETRY_WAIT)


def cleanup(settings: Settings, check_submissions: bool = True, client=None) -> list[dict]:
    """완료된 일정 삭제 (매일 자정 실행). LMS 출처 항목은 원본 DB에 완료 표시.

    삭제 전에 Canvas 에서 제출 여부를 먼저 확인한다 → 그날 낸 과제가 그날 자정에 바로 정리된다.
    확인이 실패해도(네트워크 등) 이미 완료로 표시된 일정은 그대로 삭제한다.
    """
    init_all(settings)
    if check_submissions:
        try:
            sync_lms(settings, client=client)
        except Exception:
            log.exception("삭제 전 제출 확인 실패 → 이미 완료된 일정만 삭제")
    removed = schedule_db.cleanup_completed(settings.schedule_db)
    lms_ids = [r["source_id"] for r in removed if r["source"] == SOURCE and r["source_id"]]
    if lms_ids:
        raw_db.mark_done(settings.lms_raw_db, lms_ids)
    log.info("완료 일정 %d건 삭제 (LMS %d건)", len(removed), len(lms_ids))
    return removed
