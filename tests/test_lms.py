import pytest

from common import schedule_db
from lms import raw_db
from lms.canvas_client import (html_to_text_and_links, normalize_announcement,
                               normalize_assignment, to_local)
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


def announcement(tid, title="공지", msg="<p>중간고사 10/23</p>", posted="2026-10-13T01:00:00Z"):
    return {"id": tid, "title": title, "message": msg, "posted_at": posted,
            "html_url": f"{BASE}/courses/1/discussion_topics/{tid}"}


class FakeClient:
    def __init__(self, assignments, announcements=()):
        self.assignments = assignments
        self.announcements = announcements

    def course_url(self, cid):
        return f"{BASE}/courses/{cid}"

    def get_courses(self, extra_ids=()):
        return [{"id": 1, "name": "캡스톤디자인"}]

    def iter_assignments(self, course):
        for a in self.assignments:
            yield normalize_assignment(a, course["id"], course["name"], BASE, "Asia/Seoul")

    def iter_announcements(self, course):
        for t in self.announcements:
            yield normalize_announcement(t, course["id"], course["name"], BASE, "Asia/Seoul")


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


def test_notices_saved_to_raw_only(settings):
    """게시판 공지는 raw_items 에만 저장되고 일정 테이블에는 올라가지 않는다."""
    client = FakeClient([assignment(10)], announcements=[announcement(50, title="중간고사 안내")])
    r = sync_lms(settings, client=client)
    assert (r.notices, r.notices_new) == (1, 1)

    item = raw_db.get_item(settings.lms_raw_db, "announcement:50")
    assert item["item_type"] == "announcement"
    assert item["title"] == "중간고사 안내"
    assert "중간고사" in item["content"]
    assert item["due_at"] is None
    # 일정 테이블에는 과제만 (공지는 제외)
    rows = schedule_db.list_schedules(settings.schedule_db)
    assert [row["source_id"] for row in rows] == ["assignment:10"]

    r = sync_lms(settings, client=client)  # 다시 돌려도 중복 저장 안 됨
    assert (r.notices, r.notices_new) == (1, 0)


def board(bid, title, type="notice", from_module=True, desc="<p>내용</p>"):
    return {"id": bid, "title": title, "type": type, "from_module": from_module, "description": desc}


def test_normalize_board_filters():
    from lms.learningx import normalize_board
    # 공지 게시판 → 포함 (타입/링크/본문 확인)
    it = normalize_board(board(1, "중간고사 안내", desc="<p>10/22 9시 강의실</p>"), 96043, "정보통신", BASE, 5)
    assert it["source_id"] == "lxboard:1" and it["item_type"] == "lx_board" and it["due_at"] is None
    assert "10/22" in it["content"]
    assert it["link"] == f"{BASE}/courses/96043/external_tools/5"
    # 본문이 없으면 제목을 내용으로 (예: Final exam)
    it = normalize_board(board(2, "Final exam Thursday 9AM", type="normal", desc=""), 96043, "정보통신", BASE, 5)
    assert it is not None and it["content"] == "Final exam Thursday 9AM"
    # 제외 대상들: Q&A, 수동 자료실(normal+module False), 모듈 생성 qna 토론
    assert normalize_board(board(3, "Q&A", type="qna", from_module=False), 96043, "x", BASE, 5) is None
    assert normalize_board(board(4, "강의자료실", type="normal", from_module=False, desc=""), 96043, "x", BASE, 5) is None
    assert normalize_board(board(5, "Follow-up", type="qna", from_module=True), 96043, "x", BASE, 5) is None


def test_normalize_post_and_container():
    from lms.learningx import normalize_post, _is_announcement_container
    # 'Announcement' 게시판 안의 글 → 공지 (빅데이터개론 방식)
    p = {"id": 55, "title": "[Prof. Notice] Midterm", "content": "<p>1시간 오픈북</p>"}
    it = normalize_post(p, 98769, "빅데이터개론", BASE, 5)
    assert it["source_id"] == "lxpost:55" and it["item_type"] == "lx_board"
    assert "오픈북" in it["content"]
    # 공지 글이 들어있을 법한 게시판 판정
    assert _is_announcement_container({"type": "normal", "title": "Announcement"})
    assert _is_announcement_container({"type": "notice", "title": "아무거나"})
    assert not _is_announcement_container({"type": "qna", "title": "Q&A 게시판"})
    assert not _is_announcement_container({"type": "normal", "title": "강의자료실"})


def test_sync_board_notices_saves_raw_only(settings):
    from lms.learningx import normalize_board
    from lms.sync import sync_board_notices
    raw_db.init_db(settings.lms_raw_db)
    schedule_db.init_db(settings.schedule_db)
    items = [normalize_board(board(10, "중간고사", desc="<p>10/22</p>"), 96043, "정보통신", BASE, 5)]
    r = sync_board_notices(settings, items)
    assert (r.lx_notices, r.lx_new) == (1, 1)
    item = raw_db.get_item(settings.lms_raw_db, "lxboard:10")
    assert item["item_type"] == "lx_board" and item["due_at"] is None
    assert schedule_db.list_schedules(settings.schedule_db) == []  # 일정 테이블엔 안 올라감


def test_stale_notice_pruned_when_disappeared(settings):
    sync_lms(settings, client=FakeClient([assignment(10)], announcements=[announcement(50)]))
    assert raw_db.get_item(settings.lms_raw_db, "announcement:50") is not None
    # 다음 수집에서 공지가 사라짐 → raw_items 에서 정리됨
    r = sync_lms(settings, client=FakeClient([assignment(10)], announcements=[]))
    assert r.pruned == 1
    assert raw_db.get_item(settings.lms_raw_db, "announcement:50") is None


def test_notice_not_pruned_when_fetch_fails(settings, monkeypatch):
    import lms.sync as sync_mod
    monkeypatch.setattr(sync_mod, "RETRY_WAIT", 0)
    sync_lms(settings, client=FakeClient([assignment(10)], announcements=[announcement(50)]))

    class NoAnnounce(FakeClient):
        def iter_announcements(self, course):
            raise TimeoutError("공지 서버 다운")

    r = sync_lms(settings, client=NoAnnounce([assignment(10)]))
    # 못 받은 거지 사라진 게 아니므로 삭제하지 않는다
    assert r.canvas_notices_ok is False and r.pruned == 0
    assert raw_db.get_item(settings.lms_raw_db, "announcement:50") is not None


def test_prune_respects_source_flags(settings):
    from lms.sync import SyncResult, prune_stale_notices
    raw_db.init_db(settings.lms_raw_db)
    for sid, itype in [("announcement:1", "announcement"), ("lxboard:2", "lx_board")]:
        raw_db.save_item(settings.lms_raw_db, {
            "source_id": sid, "item_type": itype, "course_id": 1, "course_name": "x",
            "title": "t", "content": "c", "content_html": "", "link": "l",
            "extracted_links": [], "due_at": None, "submitted": False})
    # 둘 다 '이번에 못 봤다'(notice_ids 비움)지만 Canvas 만 완전수집 → announcement 만 삭제
    r = SyncResult(canvas_notices_ok=True, learningx_ok=False)
    prune_stale_notices(settings, r)
    assert r.pruned == 1
    assert raw_db.get_item(settings.lms_raw_db, "announcement:1") is None
    assert raw_db.get_item(settings.lms_raw_db, "lxboard:2") is not None  # LearningX 미완료 → 보존


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

    removed = cleanup(settings, client=client)
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

    cleanup(settings, check_submissions=False)
    sync_lms(settings, full=True, client=FakeClient([assignment(10)]))
    assert schedule_db.list_schedules(settings.schedule_db) == []


def test_user_url_adds_course(settings):
    from lms.sync import user_course_ids
    raw_db.init_db(settings.lms_raw_db)
    raw_db.add_url(settings.lms_raw_db, f"{BASE}/courses/777/assignments", "user")
    raw_db.add_url(settings.lms_raw_db, "https://other.site/courses/5", "user")
    assert user_course_ids(settings) == [777]


# ---------------------------------------------------------------- 자정 삭제 전 제출 확인

@pytest.fixture
def fake_now(monkeypatch):
    """lms.sync 안의 '현재 시각'을 원하는 값으로 고정."""
    import lms.sync as sync_mod
    from datetime import datetime as real_dt

    class FakeNow(real_dt):
        current = real_dt(2026, 10, 7, 7, 0)

        @classmethod
        def now(cls, tz=None):
            return cls.current.replace(tzinfo=tz)

    monkeypatch.setattr(sync_mod, "datetime", FakeNow)
    return FakeNow


SUBMITTED = {"workflow_state": "submitted", "submitted_at": "2026-10-07T06:00:00Z"}
DUE_1007 = "2026-10-07T14:59:00Z"  # KST 10/07 23:59


def test_cleanup_checks_submission_then_deletes_same_night(settings, fake_now):
    """10/07 07:00 미제출 수집 → 오후에 제출 → 10/08 00:00 자정 작업이 제출 확인 후 바로 삭제."""
    sync_lms(settings, client=FakeClient([assignment(15, due=DUE_1007)]))
    assert schedule_db.get_by_source(settings.schedule_db, "lms", "assignment:15")["is_completed"] == 0

    fake_now.current = fake_now(2026, 10, 8, 0, 0)  # 마감(23:59) 지난 직후
    removed = cleanup(settings, client=FakeClient([assignment(15, due=DUE_1007, submission=SUBMITTED)]))
    assert [r["source_id"] for r in removed] == ["assignment:15"]
    assert raw_db.get_item(settings.lms_raw_db, "assignment:15")["is_done"] == 1


def test_submitted_after_due_marks_completed_on_next_sync(settings, fake_now):
    """마감 지난 뒤 수집이어도 이미 일정에 있는 과제는 제출 → 완료 반영 (이전 버그)."""
    sync_lms(settings, client=FakeClient([assignment(15, due=DUE_1007)]))
    fake_now.current = fake_now(2026, 10, 8, 7, 0)
    sync_lms(settings, client=FakeClient([assignment(15, due=DUE_1007, submission=SUBMITTED)]))
    assert schedule_db.get_by_source(settings.schedule_db, "lms", "assignment:15")["is_completed"] == 1


def test_past_due_not_submitted_is_still_not_added(settings, fake_now):
    """버그 수정이 '마감 지난 새 과제는 안 올린다' 규칙을 깨지 않는지."""
    fake_now.current = fake_now(2026, 10, 8, 7, 0)
    sync_lms(settings, client=FakeClient([assignment(15, due=DUE_1007)]))
    assert schedule_db.get_by_source(settings.schedule_db, "lms", "assignment:15") is None


def test_cleanup_still_deletes_when_canvas_check_fails(settings, monkeypatch):
    schedule_db.init_db(settings.schedule_db)
    sid = schedule_db.add_schedule(settings.schedule_db, "직접 추가한 일정")
    schedule_db.set_completed(settings.schedule_db, sid)

    class BrokenClient(FakeClient):
        def get_courses(self, extra_ids=()):
            raise ConnectionError("network down")

    import lms.sync as sync_mod
    monkeypatch.setattr(sync_mod, "RETRY_WAIT", 0)
    removed = cleanup(settings, client=BrokenClient([]))
    assert [r["id"] for r in removed] == [sid]


def test_retry_course_after_timeout(settings, monkeypatch):
    import lms.sync as sync_mod
    monkeypatch.setattr(sync_mod, "RETRY_WAIT", 0)

    class FlakyClient(FakeClient):
        calls = 0

        def iter_assignments(self, course):
            FlakyClient.calls += 1
            if FlakyClient.calls == 1:
                raise TimeoutError("read timed out")
            yield from super().iter_assignments(course)

    r = sync_lms(settings, client=FlakyClient([assignment(10)]))
    assert FlakyClient.calls == 2 and r.errors == [] and r.new == 1
