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

from my_scrapper import WebScrapper
from crawler.crawler_db_connect import import_schedules_from_crawler_db_to_common_db
import asyncio
import threading

DEFAULT_MODEL = "gemini-2.5-flash-lite"
# 기본 모델이 과부하(503)/한도초과(429)면 순서대로 시도. .env GEMINI_FALLBACK_MODELS=a,b 로 변경
DEFAULT_FALLBACKS = ["gemini-2.5-flash", "gemini-3.5-flash-lite"]
RETRY_CODES = {429, 500, 503, 504}
MAX_HISTORY = 10  # 기억할 최근 대화 턴 수 (user+model 쌍)

SYSTEM_RULES = """당신은 대학생의 일정 관리 비서입니다. 아래 [일정]과 [공지] 데이터를 근거로 한국어로 짧고 명확하게 답합니다.

규칙:
- 일정을 알려줄 때는 마감 임박 순으로 한 줄에 하나씩 씁니다. 예: `- D-7 | 2026-10-13(화) 23:59 | 빅데이터개론 | Week 04 Lab | https://...`
  D-day·마감·과목·제목·링크는 [일정]에 적힌 값을 그대로 복사합니다. 날짜·요일·D-day를 직접 계산하거나 바꾸지 마세요.
- 형식 설명이나 머리글 줄은 출력하지 말고 바로 답합니다.
- '이번 주'/'다음 주' 질문에는 '주' 칸이 '이번주'/'다음주'인 일정만 답합니다.
- 별도 요청이 없으면 완료된 일정과 마감 지난 일정은 빼고 말합니다.
- 시험(중간/기말)·공지·안내처럼 마감이 없는 질문에는 [공지] 데이터를 근거로 답하고, 관련 공지의 링크를 함께 보여줍니다. 시험 날짜·장소는 공지 본문에 적힌 값만 사용합니다.
- 공지 '내용'을 묻거나(예: "그 공지 내용 알려줘", "무슨 공지야?") 특정 공지를 짚어 물으면, [공지]의 '내용' 칸을 바탕으로 핵심을 2~4줄로 요약해 알려주고 링크를 붙입니다. 제목·링크만 나열하지 마세요. 내용 칸이 잘려 세부가 없으면 링크에서 전체를 확인하라고 안내합니다.
- 공지나 일정 데이터를 직접 '조회/검색'하는 함수(예: search_notices)는 없습니다. 없는 함수를 지어내지 말고, 이미 주어진 [일정]·[공지] 데이터에서 직접 찾아 글로 답하세요.
- 데이터에 없는 일정·공지는 지어내지 말고 없다고 말합니다.
- 사용자가 완료/추가/수정/삭제를 요청하면 해당 함수를 호출합니다. 날짜는 'YYYY-MM-DD HH:MM'으로 변환합니다 (시간 미지정 시 23:59).
- 일정 추가는 제목만 있으면 바로 add_schedule 을 호출합니다 (과목 등 추가 정보를 되묻지 않음).
- 완료/수정/삭제할 기존 일정이 여러 개 해당되거나 불분명하면 함수를 호출하지 말고 후보를 보여주며 되묻습니다.
- 삭제는 사용자가 '삭제/지워'라고 명시했을 때만 합니다. '끝냈어/했어/제출했어'는 완료 처리입니다.
- LMS 새로고침/다시 불러오기 요청에는 sync_lms 를 호출합니다.
- 크롤러 실행/크롤링 시작 요청에는 run_crawler 를 호출합니다.
- 크롤링의 진행 상태, 완료 여부, 결과는 절대로 임의로 추측하거나 지어내서 답하지 마세요. 사용자가 크롤링 상태나 완료 여부, 결과 등을 물어보면 반드시 get_crawl_result 도구를 호출해야 합니다.
- 크롤러가 수집한 일정을 가져오거나 DB에 반영/동기화해달라는 요청에는 import_from_crawler 를 호출합니다.
- 크롤러 대상(시작점) URL 목록 확인 요청에는 get_crawler_url_list 를 호출합니다.
- 크롤러 대상 URL 추가/삭제 요청에는 각각 add_crawler_urls, remove_crawler_urls 를 호출합니다.
"""

_INT_LIST = {"type": "array", "items": {"type": "integer"}}
_STR_LIST = {"type": "array", "items": {"type": "string"}}
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
    # 크롤러 관련
    types.FunctionDeclaration(
        name="run_crawler", description="크롤러를 실행시킴",
        parameters_json_schema={"type": "object", "properties": {}}),
    types.FunctionDeclaration(
        name="import_from_crawler", description="크롤러가 수집한 일정들을 db로 가져옴",
        parameters_json_schema={"type": "object", "properties": {}}),
    types.FunctionDeclaration(
        name="get_crawler_url_list", description="등록된 크롤링 시작점 url목록을 가져옴",
        parameters_json_schema={"type": "object", "properties": {}}),
    types.FunctionDeclaration(
        name="remove_crawler_urls", description="선택한 등록된 크롤링 시작점 url을 제거함",
        parameters_json_schema={"type": "object", "properties": {"urls": _STR_LIST}}),
    types.FunctionDeclaration(
        name="add_crawler_urls", description="제공한 url을 크롤링 시작점 url목록에 등록함",
        parameters_json_schema={"type": "object", "properties": {"urls": _STR_LIST}}),
    types.FunctionDeclaration(
        name="get_crawl_result",
        description="현재 백그라운드 크롤링이 진행 중인지, 완료되었는지 상태와 결과를 조회합니다. 사용자가 크롤링 진행 상황, 완료 여부, 결과 등을 물어볼 때 반드시 호출해야 합니다.",
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
        # 크롤러 생성
        self.crawler = WebScrapper(api_key=key, gemini_model=self.model) # 일단 기본 경로 사용(./scrapper.db)
        self.crawl_thread: threading.Thread | None = None # 진행중인 크롤러 스레드
        self.crawl_result: str | None = None
        self.crawl_error: str | None = None
        self.actions: dict[str, Callable[..., str]] = {
            "complete_schedules": self._complete, "add_schedule": self._add,
            "update_schedule": self._update, "delete_schedule": self._delete, "sync_lms": self._sync,
            # 크롤러 관련
            "get_crawler_url_list": self._get_sources,
            "import_from_crawler" : self._import_from_crawler,
            "remove_crawler_urls": self._remove_sources,
            "add_crawler_urls": self._add_sources,
            "run_crawler": self._extract_schedules_from_crawler, 
            "get_crawl_result": self._get_crawl_result,
        }

    # ------------------------------------------------------------ 함수 실행 (결과 문장은 코드가 생성)

    # 크롤러 함수들
    def _extract_schedules_from_crawler(self) -> str: # 일단 실행됬는지 여부만 반환함, 결과 및 상태는 따로 확인해야됨
        print("ScheduleAssistant:_extract_schedules_from_crawler 호출됨")
        if self.crawl_thread and self.crawl_thread.is_alive():
            return "이미 진행중인 크롤링이 있음"

        def _run():
            try:
                ret = asyncio.run(self.crawler.run())
                self.crawl_result = str(ret)
                self.crawl_error = None
            except Exception as e:
                self.crawl_error = str(e)
                self.crawl_result = None

        self.crawl_result = None
        self.crawl_error = None
        self.crawl_thread = threading.Thread(target=_run, daemon=True)
        self.crawl_thread.start()
        return "크롤링 시작됨"

    def _import_from_crawler(self) -> str:
        print("ScheduleAssistant:_import_from_crawler 호출됨")
        try:
            import_schedules_from_crawler_db_to_common_db(self.crawler, str(self.settings.schedule_db))
        except Exception as e:
            return f"크롤러로 부터 추출된 일정을 가져오는데 실패했습니다: e:{e}"
        return "크롤러로 부터 일정을 가져오기 성공"
        
    def _get_crawl_result(self) -> str:
        print("ScheduleAssistant:_get_crawl_result 호출됨")
        if self.crawl_thread is None:
            print("진행중인 크롤링이 없음")
            return "진행중인 크롤링이 없음"
        if self.crawl_thread.is_alive():
            print("아직 크롤링이 진행중임")
            return "아직 크롤링이 진행중임"
        if self.crawl_error:
            print("크롤링 실패")
            return f"크롤링 실패: {self.crawl_error}"
        print(f"크롤링 결과: {self.crawl_result}")
        return f"크롤링 결과: {self.crawl_result}"
    def _get_sources(self) -> str:
        print("ScheduleAssistant:_get_sources 호출됨")
        return ", ".join([f"{obj.url}:{obj.title}" for obj in self.crawler.get_sources()])
    def _remove_sources(self, urls:list[str]) -> str:
        print("ScheduleAssistant:_remove_sources 호출됨")
        try:
            self.crawler.remove_sources(urls)
        except Exception as e:
            return f"url제거 실패: e:{e}"
        return f"🗑️ 삭제함: {urls}"
    def _add_sources(self, urls:list[str])->str:
        print("ScheduleAssistant:_add_sources 호출됨")
        try:
            self.crawler.add_sources(urls)
        except Exception as e:
            return f"url추가 실패: e:{e}"
        return f"➕ 추가함: {urls}"


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
        # LMS 또는 크롤러 함수 가져오기
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
            system_instruction=SYSTEM_RULES + "\n[일정]\n" + build_context(
                self.settings.schedule_db),
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
