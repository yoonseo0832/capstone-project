from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

from typing_extensions import override

from ..core.session import Page
from ..crawler.url_crawler import UrlsProvider

__all__ = [
    "ContentStorage",
    "SimpleContentStorage",
    "PageContentCollector",
]

class ContentStorage(ABC):

    def collect(self, url: str, content: str) -> None:
        """content 수집"""
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        self._collect(url, content, content_hash)
    @abstractmethod
    def _collect(self, url: str, content: str, hash: str) -> None: 
        """content를 hash해서 반환"""
        pass

    @abstractmethod
    def get_content(self, url:str) -> tuple[str, str]|None: # content, hash
        """찾은 content를 반환"""
        pass

    @abstractmethod
    def remove(self, url:str) -> tuple[str, str]|None: # 제거된 것의 content, hash
        """url에 해당하는 content를 지움, 해당하는 content가 여러개 있으면 모두제거"""
        pass


class SimpleContentStorage(ContentStorage):

    def __init__(self) -> None:
        self.contents: dict[str, tuple[str, str]] = {}  # key:url, value:(content, hash)
    @override
    def _collect(self, url: str, content: str, hash:str) -> None:
        """content 수집"""
        self.contents[url] = (content, hash)
    @override
    def get_content(self, url:str) -> tuple[str, str]|None:
        """찾은 content를 반환"""
        return self.contents.get(url)
    @override
    def remove(self, url: str) -> tuple[str, str]|None: # 제거된 것의 content, hash
        """url에 해당하는 content를 지움"""
        return self.contents.pop(url, None)


from .html_cleaner import clean_html

class PageContentCollector:
    """html본문을 추출해 저장합니다"""

    def __init__(self, page: Page, url_provider: UrlsProvider, content_storage: ContentStorage) -> None:
        self._page: Page = page
        self._url_provider: UrlsProvider = url_provider
        self._content_storage: ContentStorage = content_storage

    async def _fetch_one(self, url: str) -> str:
        """url 하나를 방문해서 정제된 본문 HTML을 추출함"""
        await self._page.goto(url)
        try:
            return clean_html(await self._page.get_raw_content())
        except Exception as e:
            return f"fail to fetch, e:{e}"

    async def fetch_next(self) -> bool: # 더 가져올게 있는지 없는지 반환
        """urlProvider에서 url 하나를 받아와 본문을 추출 후 저장함"""
        if self._url_provider.is_end():
            return False

        item = self._url_provider.next()
        if item is None:
            return False

        url, title = item
        content = await self._fetch_one(url)
        self._content_storage.collect(url, content)
        return True

    async def fetch_all(self) -> None:
        """전부 추출 후 저장"""
        while await self.fetch_next():
            pass
