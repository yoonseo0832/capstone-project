"""
DB에서 특정항목들만 필터링함.
"""

from abc import ABC, abstractmethod
from typing import override
from .content_collector import ContentStorage
from ..crawler.url_crawler import UrlsProvider, UrlsCollector, UrlStorage

__all__ = [
    "ContentFilter",
    "ContentFilterByKeyword",
    "PageContentFilter",
]
    
class ContentFilter(ABC):
    @abstractmethod
    def filter(self, content: str, title: str = "") -> bool:
        pass

class ContentFilterByKeyword(ContentFilter):
    """
    키워드로 본문 및 제목 필터링
    """

    def __init__(
        self,
        allowed_keywords: list[str] | None = None,
        blocked_keywords: list[str] | None = None,
    ) -> None:
        self._allowed_keywords: list[str] | None = allowed_keywords  # 허용된 키워드
        self._blocked_keywords: list[str] | None = blocked_keywords  # 차단된 키워드

    @override
    def filter(self, content: str, title: str = "") -> bool:
        """
        차단된 키워드를 우선으로 확인하고, 그 다음 허용 키워드를 확인
        """
        target_text = f"{title} {content}".strip()
        if self._blocked_keywords is not None:
            for blocked_keyword in self._blocked_keywords:
                if blocked_keyword in target_text:
                    return False  # 차단된 키워드가 있으면 차단
        if self._allowed_keywords is not None:
            for allowed_keyword in self._allowed_keywords:
                if allowed_keyword in target_text:
                    return True
            return False  # 허용된 키워드 목록이 있을 때, 맞는 게 없으면 차단
        return True  # 차단된 키워드만 있을 때, 걸리는 게 없으면 통과


class PageContentFilter:
    """html본문을 확인해 필터조건에 맞는 url들만 지정된 url_collector에 저장함"""

    def __init__(
        self,
        urls_provider: UrlsProvider,
        content_storage: ContentStorage,
        url_collector: UrlsCollector,
        filter_models: list[ContentFilter] | ContentFilter,
    ) -> None:
        self._urls_provider: UrlsProvider = urls_provider  # 검사할 url 목록
        self._content_storage: ContentStorage = content_storage  # 본문을 가져올 곳
        self._url_collector: UrlsCollector = url_collector  # 통과된 url들을 저장할 곳
        self._filter_models: list[ContentFilter] = (
            [filter_models] if isinstance(filter_models, ContentFilter) else filter_models
        )  # 필터링 기준

    def check_next(self) -> bool:
        """
        다음 URL을 검사하여 필터 조건을 만족하면 url_collector에 저장합니다.
        검사할 항목이 남아있으면 True, 모두 처리했으면 False를 반환합니다.
        """
        # url 가져오기
        if self._urls_provider.is_end():
            return False

        item = self._urls_provider.next()
        if item is None:
            return False
        next_url, title = item

        # content_storage에서 해당 url의 content를 받아옴 ((content, hash) 튜플)
        content_data = self._content_storage.get_content(next_url)
        if content_data is None:
            print(f"PageContentFilter: content not found for {next_url}, skipping.")
            return True

        content = content_data[0]
        if not content or not content.strip():
            return True

        # 등록된 모든 필터 조건을 검사 (모두 통과해야 함)
        is_all_passed = all(
            fm.filter(content, title=title) for fm in self._filter_models
        )

        # 조건을 만족하면 전달받은 원래 title과 함께 수집 저장소로 전달
        if is_all_passed:
            self._url_collector.collect(next_url, title)

        return True

    def check_all(self) -> None:
        """남은 모든 URL을 끝까지 필터링하여 수집 저장소에 저장"""
        while self.check_next():
            pass
