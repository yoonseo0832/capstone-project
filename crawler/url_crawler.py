from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..core.session import Page
from ..utils.urls import get_clean_url
from .click_explorer import DynamicClickExplorer

__all__ = [
    "UrlStorage",
    "SimpleUrlStorage",
    "UrlsProvider",
    "SimpleUrlsProvider",
    "UrlsCollector",
    "SimpleUrlsCollector",
    "DynamicURLExplorer",
]

class UrlStorage(ABC):
    """URL 저장소 인터페이스 (인덱스 임의접근 없이 저장 및 존재 확인에 집중)"""

    @abstractmethod
    def __contains__(self, url: str) -> bool:
        """URL 존재 여부 확인"""
        pass

    @abstractmethod
    def add(self, url: str, title: str) -> None:
        """URL 및 제목 저장"""
        pass

    @abstractmethod
    def remove(self, url:str) -> tuple[str,str]|None:
        """저장된 URL 제거, 제거된거 반환(없으면 None)"""
        pass

    @abstractmethod
    def __len__(self) -> int:
        """저장된 URL의 총 개수"""
        pass

class SimpleUrlStorage(UrlStorage):
    """메모리 기반 기본 URL 저장소"""

    def __init__(self, initial_urls: list[tuple[str, str]] | None = None) -> None:
        self.items: list[tuple[str, str]] = []
        self._url_set: set[str] = set()
        if initial_urls:
            for url, title in initial_urls:
                self.add(url, title)

    def __contains__(self, url: str) -> bool:
        return url in self._url_set

    def add(self, url: str, title: str) -> None:
        if url not in self._url_set:
            self._url_set.add(url)
            self.items.append((url, title))

    def remove(self, url: str) -> tuple[str, str] | None:
        # url title 쌍 찾기
        for item_url, title in list(self.items):
            if item_url == url:
                self.items.remove((item_url, title))
                self._url_set.discard(item_url)
                return (item_url, title)
        return None

    def __len__(self) -> int:
        return len(self.items)


class UrlsProvider(ABC):
    """URL들을 순차적으로 공급하는 반복자 인터페이스"""

    @abstractmethod
    def next(self) -> tuple[str, str] | None:
        """다음 (url, title)을 반환. 더 이상 없으면 None."""
        pass

    @abstractmethod
    def is_end(self) -> bool:
        """더 이상 처리할 항목이 없는지 확인"""
        pass

    def __iter__(self) -> UrlsProvider:
        return self

    def __next__(self) -> tuple[str, str]:
        item = self.next()
        if item is None:
            raise StopIteration
        return item


class SimpleUrlsProvider(UrlsProvider):
    """SimpleUrlStorage를 주입받아 메모리에서 순차적으로 공급하는 Provider"""

    def __init__(self, storage: SimpleUrlStorage | list[tuple[str, str]]) -> None:
        if isinstance(storage, SimpleUrlStorage):
            self.storage: SimpleUrlStorage = storage
        else:
            self.storage = SimpleUrlStorage(storage)
        self._cursor: int = 0

    def next(self) -> tuple[str, str] | None:
        if self._cursor >= len(self.storage.items):
            return None
        item = self.storage.items[self._cursor]
        self._cursor += 1
        return item

    def is_end(self) -> bool:
        return self._cursor >= len(self.storage.items)

class UrlsCollector(ABC):
    def __init__(self, url_storage:UrlStorage) -> None:
        self._url_storage = url_storage
    @abstractmethod
    def collect(self, url: str, title: str) -> None:
        """데이터 수집"""
        pass

class SimpleUrlsCollector(UrlsCollector):
    def collect(self, url: str, title: str) -> None:
        self._url_storage.add(url, title)


class DynamicURLExplorer:

    def __init__(
        self,
        page: Page,
        url_storage: UrlStorage,       # url 이미 방문여부 확인용
        url_collector: UrlsCollector,  # 새로운 url방문시 url을 수집할객체
        max_depth: int = 1,
    ):
        self.page:Page = page
        self._dynamic_click_explorer: DynamicClickExplorer = DynamicClickExplorer(page)
        self._url_storage = url_storage  # 현재 방문한 url들
        self._max_depth = max_depth
        self._url_collector = url_collector

    def _is_max_depth(self) -> bool:
        print(f"DynamicURLExplorer:curdepth:{self._dynamic_click_explorer.get_depth()}, maxdepth:{self._max_depth}")
        return self._dynamic_click_explorer.get_depth() >= self._max_depth

    async def do_recursivly(self) -> None:
        page_info = await self.page.get_page_info()
        url_init = page_info.url
        # 시작주소 넣어줌
        self._url_collector.collect(url_init, page_info.title)
        # 탐색시작
        while True:
            ret_next = await self._dynamic_click_explorer.next()
            if ret_next == 0:  # 더 이상 갈곳이 없으면 종료
                return
            page_info = await self.page.get_page_info()
            url_current = page_info.url
            if ret_next == 2:
                continue
            if url_current in self._url_storage:
                await self._dynamic_click_explorer.abort()
                continue
            self._url_collector.collect(url_current, page_info.title)
            while self._is_max_depth():
                if not await self._dynamic_click_explorer.abort():
                    break

