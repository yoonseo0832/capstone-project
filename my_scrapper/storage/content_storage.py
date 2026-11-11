"""
SQLAlchemy 기반 본문 콘텐츠 저장소(ContentStorage) 구현체 모듈
"""
from __future__ import annotations

from datetime import datetime
import json
from typing import override
from sqlalchemy import select

from ..pipeline.content_collector import ContentStorage
from ..utils.urls import get_clean_url
from .database import Database, ExtractedSchedule, RawContent, UrlList

__all__ = [
    "DBContentStorage",
    "DBScheduleStorage",
]


class DBContentStorage(ContentStorage):
    """추출된 페이지 본문(HTML/해시)을 RawContent ORM 모델로 DB에 저장하는 ContentStorage 구현체"""

    def __init__(self, db: Database):
        self.db = db

    @override
    def _collect(self, url: str, content: str, hash: str) -> None:
        """수집된 본문 내용을 RawContent에 저장 (1:1 UrlList 연결)"""
        clean_url = get_clean_url(url)

        with self.db.session() as sess:
            # 1) UrlList 확인 또는 생성
            url_item = sess.execute(
                select(UrlList).where(UrlList.url == clean_url).limit(1)
            ).scalar_one_or_none()

            if url_item is None:
                url_item = UrlList(url=clean_url, title="")
                sess.add(url_item)
                sess.flush()  # url_item.id 확보

            url_item.last_check = datetime.now()

            # 2) RawContent 확인 또는 생성/업데이트
            raw = sess.execute(
                select(RawContent).where(RawContent.url_id == url_item.id).limit(1)
            ).scalar_one_or_none()

            if raw is None:
                raw = RawContent(
                    url_id=url_item.id,
                    url=clean_url,
                    content=content,
                    content_hash=hash,
                )
                sess.add(raw)
            else:
                raw.content = content
                raw.content_hash = hash
                raw.url = clean_url

    @override
    def get_content(self, url: str) -> tuple[str, str] | None:
        """DB에서 특정 URL의 콘텐츠 정보 (content, hash)를 반환"""
        clean_url = get_clean_url(url)
        with self.db.session() as sess:
            raw = sess.execute(
                select(RawContent).where(RawContent.url == clean_url).limit(1)
            ).scalar_one_or_none()

            if raw is None or raw.content is None:
                return None
            return (raw.content, raw.content_hash or "")

    @override
    def remove(self, url:str)-> tuple[str,str]|None: # content, hash
        """url에 해당하는 원문을 찾아 db에서 제거함"""
        clean = get_clean_url(url)
        with self.db.session() as sess:
            stmt = select(RawContent).where(RawContent.url == clean)
            item = sess.execute(stmt).scalar_one_or_none()
            if item:
                sess.delete(item)
                return (item.content, item.content_hash)
            return None # 아무것도 못찾은경우

class DBScheduleStorage(ContentStorage):
    """
    LLM 추출 결과(JSON 문자열)를 파싱하여 ExtractedSchedule ORM 테이블에 저장하는 전용 Storage.
    RawContent와 1:N 관계로 매핑됩니다.
    """

    def __init__(self, db: Database):
        self.db = db

    @override
    def _collect(self, url: str, content: str, hash: str) -> None:
        """추출된 JSON 문자열을 파싱하여 ExtractedSchedule 레코드로 저장"""
        clean_url = get_clean_url(url)
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = {"has_schedule": False, "summary": content}

        with self.db.session() as sess:
            # 1) 해당 URL의 RawContent 조회
            raw = sess.execute(
                select(RawContent).where(RawContent.url == clean_url).limit(1)
            ).scalar_one_or_none()

            if raw is None:
                # RawContent가 없으면 UrlList 확인 후 생성
                url_item = sess.execute(
                    select(UrlList).where(UrlList.url == clean_url).limit(1)
                ).scalar_one_or_none()
                if url_item is None:
                    url_item = UrlList(url=clean_url, title="")
                    sess.add(url_item)
                    sess.flush()
                raw = RawContent(
                    url_id=url_item.id,
                    url=clean_url,
                    content="",
                    content_hash="",
                )
                sess.add(raw)
                sess.flush()

            # JSON이 단일 객체이거나 여러 일정 리스트일 수 있으므로 유연하게 처리
            schedules_to_add: list[dict] = []
            if isinstance(parsed, list):
                schedules_to_add = [item for item in parsed if isinstance(item, dict)]
            elif isinstance(parsed, dict):
                if "schedules" in parsed and isinstance(parsed["schedules"], list):
                    schedules_to_add = [item for item in parsed["schedules"] if isinstance(item, dict)]
                else:
                    schedules_to_add = [parsed]

            for s_data in schedules_to_add:
                item_json = json.dumps(s_data, ensure_ascii=False) if len(schedules_to_add) > 1 else content
                record = ExtractedSchedule(
                    raw_content_id=raw.id,
                    url=clean_url,
                    has_schedule=bool(s_data.get("has_schedule", False)),
                    start_date=s_data.get("start_date"),
                    end_date=s_data.get("end_date"),
                    summary=s_data.get("summary"),
                    raw_json=item_json,
                )
                sess.add(record)
                
    @override
    def remove(self, url: str) -> None:
        """특정 URL에 대해 등록된 ExtractedSchedule 레코드들을 모두 삭제"""
        clean_url = get_clean_url(url)
        with self.db.session() as sess:
            schedules = list(
                sess.execute(
                    select(ExtractedSchedule).where(ExtractedSchedule.url == clean_url)
                ).scalars().all()
            )
            for s in schedules:
                sess.delete(s)

    @override
    def get_content(self, url: str) -> tuple[str, str] | None:
        """특정 URL에 대해 추출된 일정 JSON을 반환 (없으면 None)"""
        clean_url = get_clean_url(url)
        with self.db.session() as sess:
            records = list(
                sess.execute(
                    select(ExtractedSchedule).where(ExtractedSchedule.url == clean_url)
                ).scalars().all()
            )
            if not records:
                return None
            if len(records) == 1:
                return (records[0].raw_json or "", "")
            # 여러 개일 경우 JSON 배열로 반환
            all_json = [json.loads(r.raw_json) if r.raw_json else {} for r in records]
            return (json.dumps(all_json, ensure_ascii=False, indent=2), "")
