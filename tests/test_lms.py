import pytest

from common import schedule_db
from lms import raw_db
from lms.canvas_client import html_to_text_and_links, normalize_assignment, to_local
from lms.config import Settings
from lms.sync import cleanup, sync_lms

BASE = "https://mylms.example.ac.kr"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        canvas_api_url=BASE, canvas_api_key=None,
        schedule_db=tmp_path / "schedule.db", lms_raw_db=tmp_path / "lms_raw.db",
        timezone="Asia/Seoul", sync_time="07:00", cleanup_time="00:00",
        include_no_due=False, skip_past_due=True,
    )


def assignment(aid, name="과제", due="2099-12-31T14:59:00Z", desc="<p>내용</p>", submission=None):
    return {"id": aid, "name": name, "due_at": due, "description": desc,
            "html_url": f"{BASE}/courses/1/assignments/{aid}", "submission": submission}


class FakeClient:
    def __init__(self, assignments):
        self.assignments = assignments

    def course_url(self, cid):
        return f"{BASE}/courses/{cid}"

    def get_courses(self, extra_ids=()):
        return [{"id": 1, "name": "캡스톤디자인"}]

    def iter_assignments(self, course):
        for a in self.assignments:
            yield normalize_assignment(a, course["id"], course["name"], BASE, "Asia/Seoul")


# ---------------------------------------------------------------- 변환

def test_to_local_converts_utc_to_kst():
    assert to_local("2026-10-10T14:59:00Z", "Asia/Seoul") == "2026-10-10 23:59"
    assert to_local(None, "Asia/Seoul") is None


def test_html_to_text_and_links():
    text, links = html_to_text_and_links(
        '<p>보고서 <a href="/files/3">양식</a></p><a href="https://x.com/a">x</a><a href="#top">t</a>', BASE)
    assert "보고서" in text and "양식" in text
    assert links == [f"{BASE}/files/3", "https://x.com/a"]


# ---------------------------------------------------------------- 공유 일정 DB CRUD

def test_schedule_crud(settings):
    db = settings.schedule_db
    schedule_db.init_db(db)
    sid = schedule_db.add_schedule(db, "중간고사", "공학관", due_at="2026-10-20 10:00")
    assert schedule_db.get_schedule(db, sid)["title"] == "중간고사"

    schedule_db.update_schedule(db, sid, title="중간고사(변경)")
    assert schedule_db.get_schedule(db, sid)["title"] == "중간고사(변경)"
    with pytest.raises(ValueError):
        schedule_db.update_schedule(db, sid, id=99)

    schedule_db.set_completed(db, sid)
    assert schedule_db.list_schedules(db, include_completed=False) == []
    assert schedule_db.get_schedule(db, sid)["completed_at"] is not None

    assert schedule_db.delete_schedule(db, sid)
    assert schedule_db.get_schedule(db, sid) is None


# ---------------------------------------------------------------- 동기화

def test_sync_inserts_and_dedupes(settings):
    client = FakeClient([assignment(10), assignment(11, due=None), assignment(12, due="2000-01-01T00:00:00Z")])
    r = sync_lms(settings, client=client)
    assert (r.fetched, r.new, r.scheduled, r.skipped) == (3, 3, 1, 2)
    rows = schedule_db.list_schedules(settings.schedule_db)
    assert len(rows) == 1 and rows[0]["source_id"] == "assignment:10"
    assert rows[0]["due_at"] == "2099-12-31 23:59"
    assert len(raw_db.list_items(settings.lms_raw_db)) == 3  # 원본은 전부 저장
    assert raw_db.list_urls(settings.lms_raw_db)[0]["kind"] == "lms_course"

    r = sync_lms(settings, client=client)  # 다시 돌려도 중복 X, 변경 없음
    assert (r.new, r.changed, r.scheduled) == (0, 0, 0)
    assert len(schedule_db.list_schedules(settings.schedule_db)) == 1


def test_hash_detects_content_change(settings):
    sync_lms(settings, client=FakeClient([assignment(10)]))
    old_hash = raw_db.get_item(settings.lms_raw_db, "assignment:10")["content_hash"]

    r = sync_lms(settings, client=FakeClient([assignment(10, name="과제(수정)", due="2099-12-31T05:00:00Z")]))
    assert r.changed == 1
    assert raw_db.get_item(settings.lms_raw_db, "assignment:10")["content_hash"] != old_hash
    row = schedule_db.get_by_source(settings.schedule_db, "lms", "assignment:10")
    assert (row["title"], row["due_at"]) == ("과제(수정)", "2099-12-31 14:00")


def test_submission_marks_completed_and_cleanup(settings):
    sync_lms(settings, client=FakeClient([assignment(10)]))
    client = FakeClient([assignment(10, submission={"workflow_state": "submitted", "submitted_at": "x"})])
    sync_lms(settings, client=client)
    assert schedule_db.get_by_source(settings.schedule_db, "lms", "assignment:10")["is_completed"] == 1

    removed = cleanup(settings)
    assert len(removed) == 1
    assert raw_db.get_item(settings.lms_raw_db, "assignment:10")["is_done"] == 1

    # 다음날 아침 수집해도 다시 생기지 않음 (--full 이어도)
    sync_lms(settings, full=True, client=client)
    assert schedule_db.list_schedules(settings.schedule_db) == []


def test_manual_done_not_overwritten_and_not_revived(settings):
    sync_lms(settings, client=FakeClient([assignment(10)]))
    row = schedule_db.get_by_source(settings.schedule_db, "lms", "assignment:10")
    schedule_db.set_completed(settings.schedule_db, row["id"])  # 사용자가 직접 완료 체크

    sync_lms(settings, full=True, client=FakeClient([assignment(10)]))  # 미제출 상태로 다시 수집
    assert schedule_db.get_schedule(settings.schedule_db, row["id"])["is_completed"] == 1

    cleanup(settings)
    sync_lms(settings, full=True, client=FakeClient([assignment(10)]))
    assert schedule_db.list_schedules(settings.schedule_db) == []


def test_user_url_adds_course(settings):
    from lms.sync import user_course_ids
    raw_db.init_db(settings.lms_raw_db)
    raw_db.add_url(settings.lms_raw_db, f"{BASE}/courses/777/assignments", "user")
    raw_db.add_url(settings.lms_raw_db, "https://other.site/courses/5", "user")
    assert user_course_ids(settings) == [777]
