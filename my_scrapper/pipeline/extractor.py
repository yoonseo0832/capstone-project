"""
본문에서 LLM 또는 규칙을 이용해 구조화된 데이터(JSON 등)를 추출하여 저장소에 저장하는 모듈
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import json
import os
from typing import Any
import requests

from ..crawler.url_crawler import UrlsProvider
from .content_collector import ContentStorage

__all__ = [
    "ContentExtractor",
    "GeminiContentExtractor",
    "MockContentExtractor",
    "PageContentExtractor",
]


class ContentExtractor(ABC):
    """본문 내용을 분석하여 구조화된 정보(JSON 문자열 등)를 추출하는 인터페이스"""

    @abstractmethod
    def extract(self, content: str, title: str = "") -> list[str]:
        """
        본문과 제목을 받아 추출 결과 목록(JSON 문자열 리스트)을 반환합니다.
        하나의 본문에 여러 일정이 포함되어 있을 경우 개별 일정 JSON들을 원소로 갖는 리스트를 반환합니다.
        """
        pass


class GeminiContentExtractor(ContentExtractor):
    """
    Google Gemini REST API를 호출하여 공지/일정 등의 정형 JSON 데이터 목록을 추출합니다.
    """

    DEFAULT_PROMPT = """
당신은 웹페이지 공지사항 및 학사/행사 본문에서 핵심 일정을 추출하는 AI 어시스턴트입니다.
제공된 페이지의 [제목]과 [본문 내용]을 분석하여 아래 지침에 따라 JSON 배열(Array) 형식으로만 출력하세요.

[중요 지침 - 개별 공지 상세 페이지에서만 추출]:
1. [페이지 유형 판별]:
   - 반드시 사용자가 공지 목록에서 특정 항목을 클릭하여 들어간 '단일 개별 공지 상세 페이지'에서만 일정을 추출해야 합니다.
   - 만약 제공된 페이지가 여러 게시글의 번호, 제목, 작성자, 작성일 등이 나열된 '게시판 목록/리스트 페이지'라면 절대로 일정을 추출하지 말고 빈 배열 `[]`만 출력하세요.
2. [일정 추출]:
   - 개별 공지 상세 페이지의 본문에서 행사/접수/신청/모집/마감 등의 일정 정보를 분석하세요.
   - 본문에 일정이 여러 개 포함되어 있다면 각각을 배열의 요소로 나누어 추출하세요.
   - 개별 공지 페이지이지만 구체적인 일정이 포함되지 않은 일반 안내글인 경우 빈 배열 `[]`을 출력하거나 "has_schedule": false로 설정하세요.

JSON 배열 스키마:
[
  {
    "title": "공지 또는 행사의 정제된 제목 (string)",
    "has_schedule": true 또는 false (일정/마감/접수/행사 기간이 존재하는지 여부, boolean),
    "start_date": "시작 일시 (ISO-8601 포맷: YYYY-MM-DDTHH:MM:SS, 시간 불명시 YYYY-MM-DD, 없으면 null)",
    "end_date": "종료 또는 마감 일시 (ISO-8601 포맷, 없으면 null)",
    "summary": "1~2문장의 핵심 요약 (string)",
    "target": "대상자 (예: 학부생, 전체, 컴퓨터학과 학생 등, 없으면 null)",
    "action_items": ["학생이 해야 할 행동 목록 (예: 온라인 접수, 서류 제출 등)"]
  }
]
"""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-2.5-flash-lite",#"gemini-2.5-flash",
        system_instruction: str | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model
        self.system_instruction = system_instruction or self.DEFAULT_PROMPT

    def extract(self, content: str, title: str = "") -> list[str]:
        if not self.api_key:
            raise ValueError(
                "Gemini API key is required. Pass api_key or set the GEMINI_API_KEY environment variable."
            )

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"

        # 본문이 너무 긴 경우 LLM 토큰 절약을 위해 적절히 앞부분 슬라이싱 (예: 앞 8000자)
        trimmed_content = content[:8000] if len(content) > 8000 else content

        prompt = (
            f"{self.system_instruction}\n\n"
            f"[제목]: {title}\n"
            f"[본문 내용]:\n{trimmed_content}"
        )

        payload = {
            "contents": [
                {
                    "parts": [{"text": prompt}]
                }
            ],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.1,
            },
        }

        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )

        if not response.ok:
            raise RuntimeError(
                f"Gemini API request failed [{response.status_code}]: {response.text}"
            )

        data = response.json()
        try:
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(raw_text)
            if isinstance(parsed, list):
                return [json.dumps(item, ensure_ascii=False, indent=2) for item in parsed if isinstance(item, dict)]
            elif isinstance(parsed, dict):
                if "schedules" in parsed and isinstance(parsed["schedules"], list):
                    return [json.dumps(item, ensure_ascii=False, indent=2) for item in parsed["schedules"] if isinstance(item, dict)]
                return [json.dumps(parsed, ensure_ascii=False, indent=2)]
            return []
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise ValueError(f"Failed to parse Gemini JSON response: {e}, raw: {data}")


class MockContentExtractor(ContentExtractor):
    """
    API 키 없이 로컬 개발 및 테스트를 위해 간단한 규칙으로 JSON 문자열 리스트를 생성해주는 Mock 추출기
    """

    def __init__(self, multi_schedule: bool = False) -> None:
        self.multi_schedule = multi_schedule

    def extract(self, content: str, title: str = "") -> list[str]:
        # 목록/리스트 페이지는 일정 추출 대상에서 제외하고 빈 리스트 반환
        is_list_page = any(kw in f"{title} {content}" for kw in ["게시판 목록", "목록보기", "글 목록", "공지사항 목록"])
        if is_list_page:
            return []

        has_schedule = any(kw in f"{title} {content}" for kw in ["일정", "신청", "마감", "모집"])
        
        if self.multi_schedule and has_schedule:
            # 복수 일정 반환 테스트용
            s1: dict[str, Any] = {
                "title": f"{title} - 1차 접수",
                "has_schedule": True,
                "start_date": "2026-11-01",
                "end_date": "2026-11-05",
                "summary": f"{title} 1차 온라인 서류 접수 기간",
                "target": "학부생",
                "action_items": ["온라인 신청"],
            }
            s2: dict[str, Any] = {
                "title": f"{title} - 2차 면접",
                "has_schedule": True,
                "start_date": "2026-11-10",
                "end_date": "2026-11-12",
                "summary": f"{title} 2차 대면 면접 기간",
                "target": "서류 합격자",
                "action_items": ["면접 참석"],
            }
            return [
                json.dumps(s1, ensure_ascii=False, indent=2),
                json.dumps(s2, ensure_ascii=False, indent=2),
            ]

        mock_data: dict[str, Any] = {
            "title": title or "추출된 공지",
            "has_schedule": has_schedule,
            "start_date": "2026-11-01" if has_schedule else None,
            "end_date": "2026-11-15" if has_schedule else None,
            "summary": f"{title}에 대한 추출 요약입니다.",
            "target": "학부생",
            "action_items": ["공지사항 세부 내용 확인"],
        }
        return [json.dumps(mock_data, ensure_ascii=False, indent=2)]


class PageContentExtractor:
    """
    1. urls_provider에서 대상 URL을 가져와
    2. source_storage에서 본문을 읽고
    3. extractor(LLM/규칙)로 여러 개의 일정(JSON 문자열 리스트)을 추출하여
    4. target_storage에 각각 개별적으로 저장합니다.
    """

    def __init__(
        self,
        urls_provider: UrlsProvider,
        source_storage: ContentStorage,
        target_storage: ContentStorage,
        extractor: ContentExtractor,
    ) -> None:
        self._urls_provider: UrlsProvider = urls_provider
        self._source_storage: ContentStorage = source_storage
        self._target_storage: ContentStorage = target_storage
        self._extractor: ContentExtractor = extractor

    def extract_next(self) -> bool:
        """
        다음 URL의 본문을 추출하여 target_storage에 각각 저장합니다.
        처리할 항목이 남아있으면 True, 더 이상 없으면 False를 반환합니다.
        """
        if self._urls_provider.is_end():
            return False

        item = self._urls_provider.next()
        if item is None:
            return False

        url, title = item

        # 원본 본문 가져오기
        content_data = self._source_storage.get_content(url)
        if content_data is None:
            print(f"PageContentExtractor: source content not found for {url}, skipping.")
            return True

        content = content_data[0]
        if not content or not content.strip():
            return True

        try:
            # LLM API 등을 통해 추출 결과 리스트(또는 단일 문자열) 획득
            extracted = self._extractor.extract(content, title=title)
            if isinstance(extracted, str):
                extracted_list = [extracted]
            elif isinstance(extracted, list):
                extracted_list = extracted
            else:
                extracted_list = []

            # 재추출 시 중복을 방지하기 위해 해당 URL의 기존 저장 데이터를 초기화
            # self._target_storage.clear(url)
            self._target_storage.remove(url)

            # 추출된 항목들을 target_storage에 각각 개별적으로 저장
            for extracted_item in extracted_list:
                self._target_storage.collect(url, extracted_item)
        except Exception as e:
            print(f"PageContentExtractor: extraction failed for {url}: {e}")

        return True

    def extract_all(self) -> None:
        """남은 모든 URL에 대해 일괄 추출을 수행합니다."""
        while self.extract_next():
            pass
