"""Gemini API 무료 티어로 동작하는 일정 비서.

빠른 응답을 위한 설계:
- 일정 전체를 매 질문마다 프롬프트에 넣음 → 조회용 도구 호출(LLM 왕복) 없음
- 완료/추가/수정/삭제는 함수 호출로 받되, 실행 결과 안내는 코드가 직접 만듦 → LLM 호출은 항상 1번
- 스트리밍 + 생각(thinking) 끔
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Callable, Iterator

from google import genai
from google.genai import errors, types

from assistant.context import build_context
from common import schedule_db
from lms.config import Settings

DEFAULT_MODEL = "gemini-2.5-flash-lite"
# 기본 모델이 과부하(503)/한도초과(429)면 순서대로 시도. .env GEMINI_FALLBACK_MODELS=a,b 로 변경
DEFAULT_FALLBACKS = ["gemini-2.5-flash", "gemini-3.5-flash-lite"]
RETRY_CODES = {429, 500, 503, 504}
MAX_HISTORY = 10  # 기억할 최근 대화 턴 수 (user+model 쌍)

SYSTEM_RULES = """당신은 대학생의 일정 관리 비서입니다. 아래 [일정] 데이터만 근거로 한국어로 짧고 명확하게 답합니다.

규칙:
- 일정을 알려줄 때는 마감 임박 순으로 한 줄에 하나씩 씁니다. 예: `- D-7 | 10/13(화) 23:59 | 빅데이터개론 | Week 04 Lab | https://...`
  D-day·마감·과목·제목·링크는 [일정]에 적힌 값을 그대로 복사합니다. 날짜·요일·D-day를 직접 계산하거나 바꾸지 마세요.
- 형식 설명이나 머리글 줄은 출력하지 말고 바로 답합니다.
- '이번 주'/'다음 주' 질문에는 '주' 칸이 '이번주'/'다음주'인 일정만 답합니다.
- 별도 요청이 없으면 완료된 일정과 마감 지난 일정은 빼고 말합니다.
- 데이터에 없는 일정은 지어내지 말고 없다고 말합니다.
- 사용자가 완료/추가/수정/삭제를 요청하면 해당 함수를 호출합니다. 날짜는 'YYYY-MM-DD HH:MM'으로 변환합니다 (시간 미지정 시 23:59).
- 일정 추가는 제목만 있으면 바로 add_schedule 을 호출합니다 (과목 등 추가 정보를 되묻지 않음).
- 완료/수정/삭제할 기존 일정이 여러 개 해당되거나 불분명하면 함수를 호출하지 말고 후보를 보여주며 되묻습니다.
- 삭제는 사용자가 '삭제/지워'라고 명시했을 때만 합니다. '끝냈어/했어/제출했어'는 완료 처리입니다.
- LMS 새로고침/다시 불러오기 요청에는 sync_lms 를 호출합니다.
"""

_INT_LIST = {"type": "array", "items": {"type": "integer"}}
FUNCTIONS = [
    types.FunctionDeclaration(
        name="complete_schedules", description="일정들을 완료 처리한다.",
        parameters_json_schema={"type": "object", "properties": {"ids": _INT_LIST}, "required": ["ids"]}),
    types.FunctionDeclaration(
        name="add_schedule", description="새 일정을 추가한다.",
        parameters_json_schema={"type": "object", "properties": {
            "title": {"type": "string"},
            "due_at": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
            "content": {"type": "string"}, "url": {"type": "string"}}, "required": ["title"]}),
    types.FunctionDeclaration(
        name="update_schedule", description="일정의 제목/마감/내용을 수정한다. 바꿀 값만 넘긴다.",
        parameters_json_schema={"type": "object", "properties": {
            "schedule_id": {"type": "integer"}, "title": {"type": "string"},
            "due_at": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
            "content": {"type": "string"}}, "required": ["schedule_id"]}),
    types.FunctionDeclaration(
        name="delete_schedule", description="일정을 삭제한다. 사용자가 명시적으로 삭제를 요청했을 때만.",
        parameters_json_schema={"type": "object", "properties": {"schedule_id": {"type": "integer"}},
                                "required": ["schedule_id"]}),
    types.FunctionDeclaration(
        name="sync_lms", description="Canvas LMS 에서 과제를 지금 다시 불러온다.",
        parameters_json_schema={"type": "object", "properties": {}}),
]


def _parts(chunk) -> list[types.Part]:
    if not chunk.candidates or not chunk.candidates[0].content:
        return []
    return chunk.candidates[0].content.parts or []


def _check_time(value: str | None) -> str | None:
    if value is None:
        return None
    datetime.strptime(value, schedule_db.TIME_FMT)  # 형식 틀리면 ValueError
    return value


class ScheduleAssistant:
    def __init__(self, settings: Settings, api_key: str | None = None, model: str | None = None,
                 client: genai.Client | None = None):
        self.settings = settings
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        fallbacks = os.environ.get("GEMINI_FALLBACK_MODELS")
        fallbacks = [m.strip() for m in fallbacks.split(",") if m.strip()] if fallbacks else DEFAULT_FALLBACKS
        self.models = list(dict.fromkeys([self.model, *fallbacks]))  # 순서 유지 + 중복 제거
        self.last_model = self.model
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if client is None and not key:
            raise RuntimeError(".env 에 GEMINI_API_KEY 가 없습니다 (https://aistudio.google.com/apikey)")
        self.client = client or genai.Client(api_key=key)
        self.history: list[types.Content] = []
        self.actions: dict[str, Callable[..., str]] = {
            "complete_schedules": self._complete, "add_schedule": self._add,
            "update_schedule": self._update, "delete_schedule": self._delete, "sync_lms": self._sync,
        }

    # ------------------------------------------------------------ 함수 실행 (결과 문장은 코드가 생성)

    def _complete(self, ids: list[int]) -> str:
        done = [r["title"] for i in ids if (r := schedule_db.set_completed(self.settings.schedule_db, int(i)))]
        return f"✅ 완료 처리: {', '.join(done)} (자정에 목록에서 정리됩니다)" if done else "해당 일정을 찾지 못했어요."

    def _add(self, title: str, due_at: str | None = None, content: str | None = None,
             url: str | None = None) -> str:
        new_id = schedule_db.add_schedule(self.settings.schedule_db, title, content, _check_time(due_at), url=url)
        return f"➕ 추가함: [{new_id}] {title}" + (f" (마감 {due_at})" if due_at else "")

    def _update(self, schedule_id: int, title: str | None = None, due_at: str | None = None,
                content: str | None = None) -> str:
        fields = {k: v for k, v in dict(title=title, due_at=_check_time(due_at), content=content).items()
                  if v is not None}
        row = schedule_db.update_schedule(self.settings.schedule_db, int(schedule_id), **fields)
        if row is None:
            return f"{schedule_id}번 일정을 찾지 못했어요."
        return f"✏️ 수정함: [{row['id']}] {row['title']} (마감 {row['due_at'] or '없음'})"

    def _delete(self, schedule_id: int) -> str:
        row = schedule_db.get_schedule(self.settings.schedule_db, int(schedule_id))
        if row is None or not schedule_db.delete_schedule(self.settings.schedule_db, int(schedule_id)):
            return f"{schedule_id}번 일정을 찾지 못했어요."
        return f"🗑️ 삭제함: {row['title']}"

    def _sync(self) -> str:
        from lms.sync import sync_lms
        return f"🔄 LMS 동기화: {sync_lms(self.settings)}"

    def run_action(self, name: str, args: dict) -> str:
        fn = self.actions.get(name)
        if fn is None:
            return f"(알 수 없는 동작: {name})"
        try:
            return fn(**args)
        except ValueError:
            return "날짜 형식을 이해하지 못했어요. 예: 2026-10-10 23:59"
        except Exception as e:  # 비서가 죽지 않도록
            return f"처리 중 오류: {e}"

    # ------------------------------------------------------------ 대화

    def _config(self, model: str) -> types.GenerateContentConfig:
        # 2.5 계열만 thinking 을 완전히 끌 수 있다 (3.x 에 budget=0 을 주면 400 에러)
        thinking = types.ThinkingConfig(thinking_budget=0) if "2.5" in model else None
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_RULES + "\n[일정]\n" + build_context(self.settings.schedule_db),
            tools=[types.Tool(function_declarations=FUNCTIONS)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=thinking,
            temperature=0.2,
        )

    def ask(self, question: str) -> Iterator[str]:
        """답변을 조각(chunk) 단위로 흘려보낸다."""
        contents = self.history + [types.Content(role="user", parts=[types.Part(text=question)])]
        answer, calls = [], []  # 마지막으로 시도한 모델의 결과
        for i, model in enumerate(self.models):
            answer, calls = [], []
            try:
                for chunk in self.client.models.generate_content_stream(
                        model=model, contents=contents, config=self._config(model)):
                    # chunk.text 는 함수 호출이 섞이면 경고를 찍으므로 part 에서 직접 꺼낸다
                    for part in _parts(chunk):
                        if part.function_call:
                            calls.append(part.function_call)
                        elif part.text and not part.thought:
                            answer.append(part.text)
                            yield part.text
                self.last_model = model
                break
            except errors.APIError as e:
                # 과부하(503)·한도초과(429)·서버오류는 다음 모델로. 무료 한도는 모델별이라 바꾸면 대개 통과
                if e.code not in RETRY_CODES:
                    yield ("\n" if answer else "") + f"⚠️ Gemini API 오류 ({e.code}): {e.message}"
                    return
                if i == len(self.models) - 1:
                    yield ("\n" if answer else "") + "⚠️ 지금 모든 모델이 바쁘거나 무료 사용량을 초과했어요. 1분쯤 뒤에 다시 물어봐 주세요."
                    return
                if answer:  # 답하던 도중 끊겼으면 처음부터 다시
                    yield f"\n↻ 응답이 끊겨서 다시 답할게요 ({self.models[i + 1]})\n"

        for call in calls:
            result = self.run_action(call.name, dict(call.args or {}))
            prefix = "\n" if answer else ""
            answer.append(prefix + result)
            yield prefix + result

        self.history = (contents + [types.Content(role="model", parts=[types.Part(text="".join(answer))])]
                        )[-MAX_HISTORY * 2:]
