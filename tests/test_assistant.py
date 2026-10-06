from datetime import datetime
from types import SimpleNamespace

import pytest

from assistant.bot import ScheduleAssistant
from assistant.context import build_context, d_day, short_course, split_course
from common import schedule_db
from lms.config import Settings


@pytest.fixture
def settings(tmp_path):
    s = Settings(canvas_api_url="https://lms.example", canvas_api_key=None,
                 schedule_db=tmp_path / "s.db", lms_raw_db=tmp_path / "r.db", timezone="Asia/Seoul",
                 sync_time="07:00", cleanup_time="00:00", include_no_due=False, skip_past_due=True)
    schedule_db.init_db(s.schedule_db)
    return s


def test_short_course():
    assert short_course("262R (세종-학부)빅데이터개론(영강)(INTRODUCTION TO BIGDATA(English))-01분반") == "빅데이터개론"
    assert short_course("[학생] [한국어] 2026 인권과 성평등 교육 - 01분반") == "인권과 성평등 교육"
    assert short_course("262R (세종-학부)캡스톤디자인II(CAPSTONE DESIGN II)-00분반") == "캡스톤디자인II"


def test_split_course_nested_brackets():
    assert split_course("[[학생] [한국어] 교육 - 01분반] 설명") == ("[학생] [한국어] 교육 - 01분반", "설명")
    assert split_course("그냥 메모") == (None, "그냥 메모")


def test_d_day():
    now = datetime(2026, 10, 6, 21, 0)
    assert d_day("2026-10-06 23:59", now) == "D-day"
    assert d_day("2026-10-08 23:59", now) == "D-2"
    assert d_day("2026-10-06 10:00", now) == "마감지남"
    assert d_day(None, now) == "마감없음"


def test_build_context(settings):
    schedule_db.add_schedule(settings.schedule_db, "Week 04 Lab",
                             "[262R (세종-학부)빅데이터개론(영강)-01분반] 실습 제출", "2026-10-13 23:59",
                             url="https://lms/a/1", source="lms", source_id="assignment:1")
    ctx = build_context(settings.schedule_db, datetime(2026, 10, 6, 21, 0))
    assert "2026-10-06 21:00 (화)" in ctx
    assert "10/13(화) 23:59 | D-7 | 다음주 | 미완료 | 빅데이터개론 | Week 04 Lab | https://lms/a/1 | 실습 제출" in ctx


def test_week_tag():
    from assistant.context import week_tag
    now = datetime(2026, 10, 6, 21, 0)  # 화요일
    assert week_tag("2026-10-11 23:59", now) == "이번주"   # 일요일
    assert week_tag("2026-10-12 00:00", now) == "다음주"   # 다음 주 월요일
    assert week_tag("2026-10-19 00:00", now) == "이후"
    assert week_tag("2026-10-06 10:00", now) == "지남"


# ---------------------------------------------------------------- 가짜 Gemini 클라이언트

def fake_client(chunks):
    def stream(**kwargs):
        stream.last_kwargs = kwargs
        yield from chunks
    return SimpleNamespace(models=SimpleNamespace(generate_content_stream=stream)), stream


def chunk(text=None, calls=None):
    parts = [SimpleNamespace(text=None, thought=None, function_call=c) for c in calls or []]
    if text:
        parts.append(SimpleNamespace(text=text, thought=None, function_call=None))
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))])


def call(name, **args):
    return SimpleNamespace(name=name, args=args)


def test_ask_streams_text_and_keeps_history(settings):
    client, stream = fake_client([chunk("이번 주는 "), chunk("1건이에요.")])
    bot = ScheduleAssistant(settings, client=client)
    assert "".join(bot.ask("이번 주 마감?")) == "이번 주는 1건이에요."
    assert "[일정]" in stream.last_kwargs["config"].system_instruction
    assert [c.role for c in bot.history] == ["user", "model"]


def test_function_call_executes_without_second_llm_call(settings):
    sid = schedule_db.add_schedule(settings.schedule_db, "Assignment1", due_at="2026-10-07 23:59")
    client, stream = fake_client([chunk(calls=[call("complete_schedules", ids=[sid])])])
    bot = ScheduleAssistant(settings, client=client)
    out = "".join(bot.ask("Assignment1 끝냈어"))
    assert "완료 처리: Assignment1" in out
    assert schedule_db.get_schedule(settings.schedule_db, sid)["is_completed"] == 1


def busy_client(behaviors):
    """모델 이름별 동작: 'busy' 면 503, 'cut' 이면 글자 조금 보내고 503, 문자열이면 그 답변."""
    from google.genai import errors
    tried = []

    def stream(model, **kwargs):
        tried.append(model)
        b = behaviors[model]
        if b in ("busy", "cut"):
            if b == "cut":
                yield chunk("- D-1 | 10/07")
            raise errors.ServerError(503, {"error": {"code": 503, "message": "high demand"}})
        yield chunk(b)
    return SimpleNamespace(models=SimpleNamespace(generate_content_stream=stream)), tried


def test_fallback_to_next_model_on_503(settings):
    client, tried = busy_client({"m1": "busy", "m2": "답변"})
    bot = ScheduleAssistant(settings, client=client, model="m1")
    bot.models = ["m1", "m2"]
    assert "".join(bot.ask("이번 주?")) == "답변"
    assert tried == ["m1", "m2"] and bot.last_model == "m2"


def test_fallback_after_partial_answer_restarts(settings):
    client, _ = busy_client({"m1": "cut", "m2": "전체 답변"})
    bot = ScheduleAssistant(settings, client=client, model="m1")
    bot.models = ["m1", "m2"]
    out = "".join(bot.ask("이번 주?"))
    assert "응답이 끊겨서 다시 답할게요 (m2)" in out and out.endswith("전체 답변")
    assert bot.history[-1].parts[0].text == "전체 답변"  # 기록엔 끊긴 답 대신 완성된 답만


def test_all_models_busy(settings):
    client, tried = busy_client({"m1": "busy", "m2": "busy"})
    bot = ScheduleAssistant(settings, client=client, model="m1")
    bot.models = ["m1", "m2"]
    assert "모든 모델이 바쁘거나" in "".join(bot.ask("이번 주?"))
    assert tried == ["m1", "m2"] and bot.history == []


def test_fallback_models_from_env(settings, monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "a, b,a")
    bot = ScheduleAssistant(settings, client=fake_client([])[0], model="a")
    assert bot.models == ["a", "b"]


def test_actions_add_update_delete_and_bad_date(settings):
    bot = ScheduleAssistant(settings, client=fake_client([])[0])
    assert "추가함" in bot.run_action("add_schedule", {"title": "중간고사", "due_at": "2026-10-20 10:00"})
    row = schedule_db.list_schedules(settings.schedule_db)[0]
    assert "수정함" in bot.run_action("update_schedule", {"schedule_id": row["id"], "due_at": "2026-10-21 10:00"})
    assert schedule_db.get_schedule(settings.schedule_db, row["id"])["due_at"] == "2026-10-21 10:00"
    assert "날짜 형식" in bot.run_action("update_schedule", {"schedule_id": row["id"], "due_at": "내일"})
    assert "삭제함" in bot.run_action("delete_schedule", {"schedule_id": row["id"]})
    assert "찾지 못했어요" in bot.run_action("delete_schedule", {"schedule_id": 999})
