"""
SQLAlchemy 기반 URL 저장소(UrlStorage) 및 공급기(UrlsProvider) 구현체 모듈
"""
from __future__ import annotations

from typing import override
from sqlalchemy import func, select

from ..crawler.url_crawler import (
    UrlsCollector,
    UrlsProvider,
    UrlStorage,
)
from ..utils.urls import get_clean_url
from .database import Database, FilteredUrl, UrlList

__all__ = [
    "DBUrlsStorage",
    "DatabaseUrlsProvider",
    "DatabaseNotVisitedUrlsProvider",
    "DatabaseSourceUrlsProvider",
    "DatabaseFilteredUrlsProvider",
    "DatabaseUrlsCollector",
    "DatabaseDeactivatedUrlsProvider",
]


class DBUrlsStorage(UrlStorage):
    """SQLAlchemy ORM 모델 기반 UrlStorage 구현체"""

    def __init__(
        self,
        db: Database,
        model_type: type[UrlList | FilteredUrl] = UrlList,
        is_source: bool = False,
    ):
        self.db = db
        self.model_type = model_type
        self.is_source = is_source

    @override
    def __contains__(self, url: str) -> bool:
        clean = get_clean_url(url)
        with self.db.session() as sess:
            stmt = select(func.count()).select_from(self.model_type).where(self.model_type.url == clean)
            return (sess.execute(stmt).scalar() or 0) > 0

    @override
    def add(self, url: str, title: str = "") -> None:
        clean = get_clean_url(url)
        with self.db.session() as sess:
            existing = sess.execute(
                select(self.model_type).where(self.model_type.url == clean).limit(1)
            ).scalar_one_or_none()
            if existing is None:
                if self.model_type is UrlList:
                    record = UrlList(url=clean, title=title or "", is_source=self.is_source)
                elif self.model_type is FilteredUrl:
                    url_item = sess.execute(
                        select(UrlList).where(UrlList.url == clean).limit(1)
                    ).scalar_one_or_none()
                    if url_item is None:
                        url_item = UrlList(url=clean, title=title or "")
                        sess.add(url_item)
                        sess.flush()
                    record = FilteredUrl(
                        url_id=url_item.id,
                        url=clean,
                        title=title or url_item.title or "",
                    )
                else:
                    record = self.model_type(url=clean, title=title or "")
                sess.add(record)
            elif not existing.title and title:
                existing.title = title

    @override
    def remove(self, url:str)->tuple[str,str]|None:
        clean = get_clean_url(url)
        with self.db.session() as sess:
            stmt = select(self.model_type).where(
                self.model_type.url == clean
            )
            if self.is_source: # is_source면 source인 애들만 제거하도록 함
                stmt = stmt.where(UrlList.is_source == True)
            item = sess.execute(stmt).scalar_one_or_none()
            if item:
                sess.delete(item)
                return (item.url, item.title)
            return None # 아무것도 못찾은경우

    @override
    def __len__(self) -> int:
        """저장된 URL의 총 개수"""
        with self.db.session() as sess:
            stmt = select(func.count()).select_from(self.model_type)
            return sess.execute(stmt).scalar() or 0


class DatabaseUrlsProvider(UrlsProvider):
    """
    DBUrlsStorage를 주입받아 Keyset(after_id) 페이지네이션으로 URL을 공급하는 Provider.
    - storage 또는 db 객체를 인자로 받아 초기화할 수 있습니다.
    - DB에서 fetch_limit 단위로 묶어서(chunk) 큐에 가져와 반복자처럼 순회합니다.
    """

    def __init__(
        self,
        storage: DBUrlsStorage | Database,
        fetch_limit: int = 500,
    ) -> None:
        if isinstance(storage, DBUrlsStorage):
            self.storage: DBUrlsStorage = storage
            self.db: Database = storage.db
        else:
            self.storage = DBUrlsStorage(storage)
            self.db = storage

        self.queue: list[tuple[str, str]] = []
        self.fetch_limit: int = fetch_limit
        self.last_id: int = 0

    def _select(self) -> list[UrlList]:
        """기본적으로 저장된 모든 URL을 순회하며, 서브클래스에서 특정 조건으로 오버라이드할 수 있습니다."""
        with self.db.session() as sess:
            stmt = (
                select(self.storage.model_type)
                .where(self.storage.model_type.id > self.last_id)
                .order_by(self.storage.model_type.id.asc())
                .limit(self.fetch_limit)
            )
            return list(sess.execute(stmt).scalars().all())

    def _peek(self) -> None:
        """큐가 비었을 때 DB에서 다음 청크를 가져옵니다."""
        if len(self.queue) <= 0:
            models = self._select()
            if models:
                self.last_id = max(m.id or 0 for m in models)
                self.queue.extend(
                    (m.url, m.title or "") for m in models if m.url
                )

    @override
    def next(self) -> tuple[str, str] | None:
        """DB에서 다음 대상 URL (url, title)을 가져옵니다. 없으면 None 반환."""
        self._peek()
        if len(self.queue) <= 0:
            return None
        return self.queue.pop(0)

    @override
    def is_end(self) -> bool:
        """더 이상 처리할 URL이 남아있는지 확인합니다."""
        self._peek()
        return len(self.queue) <= 0

class DatabaseDeactivatedUrlsProvider(DatabaseUrlsProvider):
    """DB에서 비활성화된 URL들을 순환하는 Provider"""
    @override
    def _select(self) -> list[UrlList]:
        return self.db.select_deactivated_urls(self.fetch_limit, self.last_id)

class DatabaseNotVisitedUrlsProvider(DatabaseUrlsProvider):
    """DB에서 아직 본문(RawContent)이 수집되지 않은 UrlList 목록을 순서대로 읽어오는 Provider"""

    @override
    def _select(self) -> list[UrlList]:
        return self.db.select_not_visited(limit=self.fetch_limit, after_id=self.last_id)


class DatabaseSourceUrlsProvider(DatabaseUrlsProvider):
    """DB에서 시작 URL (is_source == True) 목록을 순환하는 Provider"""

    @override
    def _select(self) -> list[UrlList]:
        with self.db.session() as sess:
            stmt = (
                select(self.storage.model_type)
                .where(
                    self.storage.model_type.is_source == True,
                    self.storage.model_type.id > self.last_id,
                )
                .order_by(self.storage.model_type.id.asc())
                .limit(self.fetch_limit)
            )
            return list(sess.execute(stmt).scalars().all())


class DatabaseFilteredUrlsProvider(DatabaseUrlsProvider):
    """DB에서 필터링을 통과한 FilteredUrl 목록을 순회하는 Provider"""

    def __init__(
        self,
        storage: DBUrlsStorage | Database,
        fetch_limit: int = 500,
        unextracted_only: bool = False,
    ):
        if isinstance(storage, DBUrlsStorage):
            super().__init__(storage, fetch_limit=fetch_limit)
        else:
            db_storage = DBUrlsStorage(storage, model_type=FilteredUrl)
            super().__init__(db_storage, fetch_limit=fetch_limit)
        self.unextracted_only = unextracted_only

    @override
    def _select(self) -> list[FilteredUrl]:
        if self.unextracted_only:
            return self.db.select_unextracted_filtered_urls(limit=self.fetch_limit, after_id=self.last_id)
        return self.db.select_filtered_urls(limit=self.fetch_limit, after_id=self.last_id)


class DatabaseUrlsCollector(UrlsCollector):
    """새로 발견한 URL을 DB에 저장하는 Collector"""

    def __init__(self, storage: DBUrlsStorage | Database, is_source: bool = False):
        if isinstance(storage, DBUrlsStorage):
            self.storage: DBUrlsStorage = storage
            self.db: Database = storage.db
        else:
            self.storage = DBUrlsStorage(storage, is_source=is_source)
            self.db = storage
        super().__init__(self.storage)

    @override
    def collect(self, url: str, title: str = "") -> None:
        """발견된 URL과 제목을 DB에 INSERT"""
        self.storage.add(url, title)

    def get_provider(self) -> DatabaseNotVisitedUrlsProvider:
        """저장된 URL들을 순회할 Provider를 반환"""
        return DatabaseNotVisitedUrlsProvider(self.storage)
