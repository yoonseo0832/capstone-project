"""
웹스크래핑, 본문 수집, 필터링, LLM 일정 추출의 전 과정을 하나로 통합한 오케스트레이터 모듈
"""
from __future__ import annotations

import os
from typing import Any

from .core.session import Context, Page
from .crawler.url_crawler import (
    DynamicURLExplorer,
    UrlsCollector,
    UrlsProvider,
    UrlStorage,
)
from .pipeline.content_collector import PageContentCollector
from .pipeline.extractor import (
    ContentExtractor,
    GeminiContentExtractor,
    MockContentExtractor,
    PageContentExtractor,
)
from .pipeline.filter import (
    ContentFilter,
    ContentFilterByKeyword,
    PageContentFilter,
)
from .storage.content_storage import DBContentStorage, DBScheduleStorage
from .storage.database import (
    Database,
    ExtractedSchedule,
    FilteredUrl,
    RawContent,
    UrlList,
)
from .storage.url_storage import (
    DBUrlsStorage,
    DatabaseFilteredUrlsProvider,
    DatabaseNotVisitedUrlsProvider,
    DatabaseUrlsCollector,
    DatabaseUrlsProvider,
    DatabaseDeactivatedUrlsProvider
)
from .utils.urls import get_clean_url

__all__ = [
    "WebScrapper",
    "MiguScrapper",
    "ScrapperPipeline",
]


class WebScrapper:
    """
    모든 크롤링 및 파이프라인 단계를 하나로 통합 관리하는 클래스.

    전체 일괄 실행(`run()`)뿐만 아니라, 각 단계를 개별적으로 실행하여
    중단된 지점부터 이어서 작업할 수 있는 독립 인터페이스를 제공합니다:
      - 1단계: URL 재귀 탐색 (`crawl()`)
      - 2단계: HTML 원문 수집 (`fetch_contents()`)
      - 3단계: 키워드 공지 필터링 (`filter_urls()`)
      - 4단계: LLM 일정 정보 추출 (`extract_schedules()`)
    """

    def __init__(
        self,
        db_path: str = "scrapper.db",
        filters: list[ContentFilter] | ContentFilter | list[str] | None = None,
        api_key: str | None = None,
        *,
        gemini_model: str = "gemini-2.5-flash",
        session_path: str | None = "./session.json",
        max_depth: int = 1,
    ) -> None:
        """
        :param db_path: 데이터베이스 파일 경로 (또는 ":memory:")
        :param filters: ContentFilter 인스턴스, 필터 리스트, 또는 키워드 문자열 리스트
        :param api_key: Gemini API 키 (None일 경우 환경변수 GEMINI_API_KEY 확인 후 Mock 대체)
        :param gemini_model: 사용할 Gemini 모델명
        :param session_path: 브라우저 세션 저장 파일 경로
        :param max_depth: URL 재귀 탐색 깊이
        """
        self.db = Database(db_path)
        self.db_path = db_path
        self.session_path = session_path
        self.max_depth = max_depth

        # 1. 필터 구성
        if filters is None:
            # 기본 학사/공지/행사 추천 키워드
            self.filter_models: list[ContentFilter] = [
                ContentFilterByKeyword(
                    allowed_keywords=[
                        "공지", "안내", "모집", "신청", "마감", "접수", "선발", "일정", "등록",
                        "세미나", "특강", "설명회", "해커톤", "공모전", "대회", "워크숍",
                        "장학", "인턴", "채용", "추천채용"
                    ]
                )
            ]
        elif isinstance(filters, list) and all(isinstance(x, str) for x in filters):
            self.filter_models = [ContentFilterByKeyword(allowed_keywords=filters)]
        elif isinstance(filters, ContentFilter):
            self.filter_models = [filters]
        else:
            self.filter_models = list(filters)

        # 2. LLM 추출기 구성
        resolved_api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if resolved_api_key:
            self.extractor: ContentExtractor = GeminiContentExtractor(
                api_key=resolved_api_key,
                model=gemini_model,
            )
        else:
            print("[!] Gemini API 키가 제공되지 않아 MockContentExtractor로 동작합니다(디버깅용).")
            self.extractor = MockContentExtractor()

        # 3. 저장소 및 수집기 인스턴스
        self.urls_storage = DBUrlsStorage(self.db)
        self.urls_collector = DatabaseUrlsCollector(self.urls_storage)
        self.filtered_storage = DBUrlsStorage(self.db, model_type=FilteredUrl)
        self.filtered_collector = DatabaseUrlsCollector(self.filtered_storage)
        self.content_storage = DBContentStorage(self.db)
        self.schedule_storage = DBScheduleStorage(self.db)

    def add_source(self, url: str, title: str = "") -> None:
        """크롤링을 시작할 Source URL 등록"""
        clean = get_clean_url(url)
        source_storage = DBUrlsStorage(self.db, is_source=True) # is_source가 true인 행만 다루는 저장소 생성
        source_storage.add(clean, title)
    def remove_source(self, url: str) -> None:
        """등록되어있는 Source URL 제거"""
        clean = get_clean_url(url)
        source_storage = DBUrlsStorage(self.db, is_source=True)
        source_storage.remove(clean)

    def add_sources(self, urls: list[str] | list[tuple[str, str]]) -> None:
        """여러 개의 Source URL을 일괄 등록"""
        for item in urls:
            if isinstance(item, tuple):
                u, t = item
                self.add_source(u, t)
            else:
                self.add_source(item)
    def remove_sources(self, urls: list[str]) -> None:
        """등록되어있는 Source URL 제거"""
        for url in urls:
            self.remove_source(url)
    def get_sources(self) -> list[UrlList]:
        """등록되어 있는 source url들을 가져옴"""
        return self.db.select_sources()

    def deactivate_url(self,url:str):
        """수집된 url을 비활성화 합니다(추출목록에서 제외됨)"""
        clean = get_clean_url(url)
        self.db.deactivate_urls([clean])
    def deactivate_urls(self,urls:list[str]):
        cleans = [get_clean_url(url) for url in urls]
        self.db.deactivate_urls(cleans)

    def remove_deactivated_things(self):
        """비활성화된 url과 관련된 것들을(추출된 본문,일정) 제거합니다"""
        deactivated_urls_storage = DatabaseDeactivatedUrlsProvider(self.db)  # 비활성화 된 url들을 순회함
        while not deactivated_urls_storage.is_end():
            current = deactivated_urls_storage.next()
            if not current:
                break  # 더이상 비활성화된 url이 없을때
            cur_url, _ = current
            # 외래키 제약조건 방지를 위해 자식(ExtractedSchedule) -> 부모(RawContent) 순으로 삭제
            self.schedule_storage.remove(cur_url)
            self.content_storage.remove(cur_url)
            self.filtered_storage.remove(cur_url)

    def remove_extracted_schedule(self, url:str):
        """특정 url에 대해서 추출된 일정 제거(여러게면 전부제거)"""
        clean = get_clean_url(url)
        self.schedule_storage.remove(clean)
    def remove_extracted_schedules(self, urls:list[str]):
        for url in urls:
            self.remove_extracted_schedule(url)


    # =========================================================================
    # 각 단계별 독립 실행 인터페이스 (이어하기 가능)
    # =========================================================================

    async def crawl(
        self,
        sources: list[str] | None = None,
        max_depth: int | None = None,
    ) -> int:
        """
        [Step 1] 시작 URL들로부터 하위 URL을 재귀적으로 탐색하여 UrlList에 저장합니다.
        (이미 수집된 URL은 중복되지 않으므로 언제든 이어서 실행 가능)

        :param sources: 특정 시작 URL 목록 (생략 시 DB에 등록된 시작 URL 전체 사용)
        :param max_depth: 탐색 깊이 (생략 시 self.max_depth 사용)
        :return: 이번 단계에서 새로 추가된 URL 수
        """
        before_count = len(self.urls_storage)
        target_depth = max_depth if max_depth is not None else self.max_depth

        target_sources = sources or [s.url for s in self.db.select_sources()]
        if not target_sources:
            print("[Step 1] 등록된 시작 URL이 없어 탐색을 종료합니다.")
            return 0

        print(f"\n[Step 1] URL 탐색 시작 (대상: {len(target_sources)}개, max_depth={target_depth})...")
        context = await Context.create(self.session_path)
        page = await context.new_page()

        try:
            for src_url in target_sources:
                print(f"  -> 시작 URL 이동: {src_url}")
                try:
                    await page.goto(src_url)
                    explorer = DynamicURLExplorer(
                        page=page,
                        url_storage=self.urls_storage,
                        url_collector=self.urls_collector,
                        max_depth=target_depth,
                    )
                    await explorer.do_recursivly()
                except Exception as e:
                    print(f"  [!] {src_url} 탐색 중 오류 발생: {e}")
        finally:
            await context.close()

        after_count = len(self.urls_storage)
        added = after_count - before_count
        print(f"[Step 1 완료] 새로 수집된 URL: {added}개 (총 {after_count}개)")
        return added

    async def fetch_contents(self) -> int:
        """
        [Step 2] 아직 방문하지 않은(RawContent가 없는) UrlList를 방문하여 본문 HTML을 수집합니다.
        (이전에 실패하거나 중단되었더라도 미방문 URL만 골라 이어서 작업합니다)

        :return: 이번 단계에서 새로 수집한 본문 수
        """
        before_stats = self.get_stats()
        not_visited_provider = DatabaseNotVisitedUrlsProvider(self.urls_storage)

        if not_visited_provider.is_end():
            print("[Step 2] 수집할 미방문 URL이 없습니다. (모두 수집 완료됨)")
            return 0

        print("\n[Step 2] 미방문 URL 대상 HTML 본문 수집 시작...")
        context = await Context.create(self.session_path)
        page = await context.new_page()

        try:
            collector = PageContentCollector(
                page=page,
                url_provider=not_visited_provider,
                content_storage=self.content_storage,
            )
            await collector.fetch_all()
        finally:
            await context.close()

        after_stats = self.get_stats()
        added = after_stats["raw_contents"] - before_stats["raw_contents"]
        print(f"[Step 2 완료] 새로 수집된 본문: {added}개 (총 {after_stats['raw_contents']}개)")
        return added

    def filter_urls(self, filter_models: list[ContentFilter] | None = None) -> int:
        """
        [Step 3] 수집된 본문 내용을 키워드 필터로 검사하여 통과한 공지 URL을 FilteredUrl 테이블에 저장합니다.
        (브라우저를 띄우지 않고 로컬 DB 본문만 순회하므로 매우 빠릅니다)

        :param filter_models: 적용할 필터 목록 (생략 시 self.filter_models 사용)
        :return: 이번 단계에서 새로 선별된 공지 URL 수
        """
        before_count = len(self.filtered_storage)
        print("\n[Step 3] 공지/일정 키워드 필터링 시작...")

        all_urls_provider = DatabaseUrlsProvider(self.urls_storage)
        filter_pipeline = PageContentFilter(
            urls_provider=all_urls_provider,
            content_storage=self.content_storage,
            url_collector=self.filtered_collector,
            filter_models=filter_models or self.filter_models,
        )
        filter_pipeline.check_all()

        after_count = len(self.filtered_storage)
        added = after_count - before_count
        print(f"[Step 3 완료] 새로 선별된 공지 URL: {added}개 (총 {after_count}개)")
        return added

    def extract_schedules(self, unextracted_only: bool = True) -> int:
        """
        [Step 4] FilteredUrl 테이블의 공지들에 대해 LLM(또는 Mock)을 호출하여 일정 정보를 추출합니다.
        
        :param unextracted_only: True일 경우 아직 일정이 추출되지 않은 공지만 골라서 추출합니다 (API 비용/시간 절약 및 이어하기 지원).
                                 False일 경우 기존 추출 여부와 관계없이 전체 FilteredUrl을 재추출합니다.
        :return: 이번 단계에서 새로 추출된 일정 레코드 수
        """
        before_stats = self.get_stats()
        print(f"\n[Step 4] LLM 일정 정보 추출 시작 (unextracted_only={unextracted_only})...")

        filtered_provider = DatabaseFilteredUrlsProvider(
            self.filtered_storage,
            unextracted_only=unextracted_only,
        )

        if filtered_provider.is_end():
            print("[Step 4] 추출 대상 공지 URL이 없습니다. (모두 추출 완료됨)")
            return 0

        extractor_pipeline = PageContentExtractor(
            urls_provider=filtered_provider,
            source_storage=self.content_storage,
            target_storage=self.schedule_storage,
            extractor=self.extractor,
        )
        extractor_pipeline.extract_all()

        after_stats = self.get_stats()
        added = after_stats["extracted_schedules"] - before_stats["extracted_schedules"]
        print(f"[Step 4 완료] 새로 추출된 일정: {added}개 (총 {after_stats['extracted_schedules']}개)")
        return added

    # 단계별 메서드 별칭 지원
    step_crawl = crawl
    step_fetch_contents = fetch_contents
    step_filter = filter_urls
    step_extract = extract_schedules

    # =========================================================================
    # 전체 통합 일괄 실행
    # =========================================================================

    async def run(
        self,
        skip_crawl: bool = False,
        skip_content_fetch: bool = False,
        skip_filter: bool = False,
        skip_extract: bool = False,
        unextracted_only: bool = True,
    ) -> dict[str, int]:
        """
        전체 크롤링 및 파이프라인 단계를 순차적으로 일괄 실행합니다.

        :param skip_crawl: 1단계 URL 탐색 건너뛰기
        :param skip_content_fetch: 2단계 본문 수집 건너뛰기
        :param skip_filter: 3단계 키워드 필터링 건너뛰기
        :param skip_extract: 4단계 LLM 일정 추출 건너뛰기
        :param unextracted_only: 4단계 실행 시 미추출 공지만 처리할지 여부
        :return: 단계별 처리 결과 요약 통계
        """
        print("=" * 60)
        print("🚀 [MiguScrapper] 전체 파이프라인 일괄 실행 시작")
        print("=" * 60)

        # 1단계: URL 탐색
        if not skip_crawl:
            await self.crawl()

        # 2단계: 본문 수집
        if not skip_content_fetch:
            await self.fetch_contents()

        # 3단계: 공지 필터링
        if not skip_filter:
            self.filter_urls()

        # 4단계: 일정 추출
        if not skip_extract:
            self.extract_schedules(unextracted_only=unextracted_only)

        # 요약 리포트 출력
        stats = self.get_stats()
        print("\n" + "=" * 60)
        print("🎯 [MiguScrapper] 파이프라인 처리 완료 요약")
        print("=" * 60)
        print(f"  - 등록된 시작 URL (Sources): {stats['sources']}개")
        print(f"  - 총 수집된 URL (UrlList): {stats['total_urls']}개")
        print(f"  - 본문 수집 완료 (RawContent): {stats['raw_contents']}개")
        print(f"  - 필터링 통과 URL (FilteredUrl): {stats['filtered_urls']}개")
        print(f"  - 추출된 일정 레코드 (ExtractedSchedule): {stats['extracted_schedules']}개")
        print("=" * 60)

        return stats

    def get_stats(self) -> dict[str, int]:
        """현재 DB에 저장된 각 테이블별 레코드 수 통계 반환"""
        with self.db.session() as sess:
            from sqlalchemy import func, select
            total_sources = sess.execute(
                select(func.count()).select_from(UrlList).where(UrlList.is_source == True)
            ).scalar() or 0
            total_urls = sess.execute(
                select(func.count()).select_from(UrlList)
            ).scalar() or 0
            raw_contents = sess.execute(
                select(func.count()).select_from(RawContent)
            ).scalar() or 0
            filtered_urls = sess.execute(
                select(func.count()).select_from(FilteredUrl)
            ).scalar() or 0
            schedules = sess.execute(
                select(func.count()).select_from(ExtractedSchedule)
            ).scalar() or 0

        return {
            "sources": total_sources,
            "total_urls": total_urls,
            "raw_contents": raw_contents,
            "filtered_urls": filtered_urls,
            "extracted_schedules": schedules,
        }

    def get_schedules(self, has_schedule_only: bool = True) -> list[ExtractedSchedule]:
        """추출된 일정 목록을 조회"""
        with self.db.session() as sess:
            from sqlalchemy import select
            stmt = select(ExtractedSchedule)
            if has_schedule_only:
                stmt = stmt.where(ExtractedSchedule.has_schedule == True)
            stmt = stmt.order_by(ExtractedSchedule.id.asc())
            return list(sess.execute(stmt).scalars().all())

    def get_crawled_urls(self) -> list[UrlList]:
        """전체 크롤링된 URL 목록 조회"""
        with self.db.session() as sess:
            from sqlalchemy import select
            stmt = select(UrlList).where(UrlList.is_active==True).order_by(UrlList.id.asc())
            return list(sess.execute(stmt).scalars().all())

    def get_filtered_urls(self) -> list[FilteredUrl]:
        """필터링을 통과한 공지 URL 목록 조회"""
        with self.db.session() as sess:
            from sqlalchemy import select
            stmt = select(FilteredUrl).order_by(FilteredUrl.id.asc())
            return list(sess.execute(stmt).scalars().all())

    def close(self) -> None:
        """데이터베이스 연결 종료"""
        self.db.close()


# 별칭 지원
MiguScrapper = WebScrapper
ScrapperPipeline = WebScrapper
