"""
SQLAlchemy 2.0 기반 데이터베이스 엔진 및 ORM 모델 모듈
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import os
from typing import Generator
from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    select,
    update,
    ForeignKey
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
    relationship
)

__all__ = [
    "Base",
    "UrlList",
    "FilteredUrl",
    "RawContent",
    "ExtractedSchedule",
    "Database",
]

class Base(DeclarativeBase):
    pass

class UrlList(Base):
    """수집 시작점 URL, 하위 URL"""

    __tablename__ = "url"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    url: Mapped[str] = mapped_column(
        String, unique=True, index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String, default="", nullable=False)
    is_source: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    last_check: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    is_active: Mapped[bool] = mapped_column( # url 활성화 여부, 활성화된 url들에 대해서만 본문,일정을 추출함
        Boolean, default=True, nullable=False
    )
    # 1:1 관계 (UrlList <-> RawContent)
    # 단일 객체 매핑이므로 타입 어노테이션에 List 대신 단일 모델 지정
    raw_content: Mapped["RawContent | None"] = relationship(
        "RawContent", back_populates="url_item", uselist=False
    )

    # 1:1 관계 (UrlList <-> FilteredUrl)
    filtered_url: Mapped["FilteredUrl | None"] = relationship(
        "FilteredUrl", back_populates="url_item", uselist=False
    )

    def __repr__(self) -> str:
        return f"<UrlList(id={self.id}, url='{self.url}', is_source={self.is_source}, last_check={self.last_check})>"


class FilteredUrl(Base):
    """필터링(공지/모집 등)을 통과한 선별된 대상 URL 테이블"""

    __tablename__ = "filtered_url"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    # 1) UrlList와의 1:1 외래키 연결
    url_id: Mapped[int] = mapped_column(
        ForeignKey("url.id"), unique=True, nullable=False
    )

    url: Mapped[str] = mapped_column(
        String, unique=True, index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String, default="", nullable=False)

    # 관계 정의 (UrlList로 역참조)
    url_item: Mapped["UrlList"] = relationship(
        "UrlList", back_populates="filtered_url"
    )

    def __repr__(self) -> str:
        return f"<FilteredUrl(id={self.id}, url_id={self.url_id}, url='{self.url}', title='{self.title}')>"


class RawContent(Base):
    """HTML 원문"""

    __tablename__ = "raw_content"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    # 1) UrlList와의 1:1 연결용 외래키
    # unique=True를 주어야 DB 레벨에서도 1:1 관계가 강제됩니다.
    url_id: Mapped[int] = mapped_column(
        ForeignKey("url.id"), unique=True, nullable=False
    )

    # 문자열 url은 UrlList에 이미 있으므로 중복 저장을 방지하거나 참고용으로 유지
    url: Mapped[str] = mapped_column(
        String, unique=True, index=True, nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # 관계 정의 (UrlList로 역참조)
    url_item: Mapped["UrlList"] = relationship(
        "UrlList", back_populates="raw_content"
    )

    # 2) ExtractedSchedule과의 1:N 연결 (원문 1개 -> 여러 개의 일정 추출)
    schedules: Mapped[list["ExtractedSchedule"]] = relationship(
        "ExtractedSchedule", back_populates="raw_content"
    )

    def __repr__(self) -> str:
        return f"<RawContent(id={self.id}, url_id={self.url_id}, hash='{self.content_hash}')>"


class ExtractedSchedule(Base):
    """LLM을 통해 추출된 구조화된 공지 및 일정 정보 테이블"""

    __tablename__ = "extracted_schedules"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    # RawContent와의 1:N 연결용 외래키 (N쪽 테이블에 외래키 배치)
    raw_content_id: Mapped[int] = mapped_column(
        ForeignKey("raw_content.id"), nullable=False, index=True
    )

    # 필요 시 보관용 문자열 url (조회 편의용)
    url: Mapped[str] = mapped_column(String, index=True, nullable=False)
    has_schedule: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    start_date: Mapped[str | None] = mapped_column(String, nullable=True)
    end_date: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 관계 정의 (RawContent로 역참조)
    raw_content: Mapped["RawContent"] = relationship(
        "RawContent", back_populates="schedules"
    )

    def __repr__(self) -> str:
        return f"<ExtractedSchedule(id={self.id}, raw_id={self.raw_content_id}, has_schedule={self.has_schedule})>"

class Database:
    """
    SQLAlchemy 기반 데이터베이스 관리자
    """

    def __init__(self, db_path: str = "scrapper.db", echo: bool = False) -> None:
        self.db_path = db_path

        if db_path == ":memory:":
            db_url = "sqlite:///:memory:"
        elif db_path.startswith("sqlite://") or db_path.startswith("postgresql://"):
            db_url = db_path
        else:
            dirname = os.path.dirname(db_path)
            if dirname and not os.path.exists(dirname):
                os.makedirs(dirname, exist_ok=True)
            db_url = f"sqlite:///{os.path.abspath(db_path)}"

        self.engine = create_engine(db_url, echo=echo)
        self.SessionLocal = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )

        # 테이블 자동 생성
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        """자동 커밋/롤백을 지원하는 세션 컨텍스트 매니저"""
        sess = self.SessionLocal()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    def select_by_url(self, url: str) -> UrlList | None:
        """URL 기준 단일 UrlList 조회"""
        with self.session() as sess:
            stmt = select(UrlList).where(UrlList.url == url).limit(1)
            return sess.execute(stmt).scalar_one_or_none()

    def select_raw_content_by_url(self, url: str) -> RawContent | None:
        """URL 기준 단일 RawContent 조회"""
        with self.session() as sess:
            stmt = select(RawContent).where(RawContent.url == url).limit(1)
            return sess.execute(stmt).scalar_one_or_none()

    def select_not_visited(self, limit: int = 500, after_id: int = 0) -> list[UrlList]:
        """
        아직 본문(RawContent)이 수집되지 않은 UrlList 목록 조회 (Keyset Pagination)
        RawContent와 Outer Join하여 RawContent가 없는 미수집 UrlList를 선별합니다.
        """
        with self.session() as sess:
            stmt = (
                select(UrlList)
                .outerjoin(RawContent, UrlList.id == RawContent.url_id)
                .where(
                    RawContent.id.is_(None), 
                    UrlList.is_active == True, # 활성화된 url들에 대해서만 수행
                    UrlList.id > after_id)
                .order_by(UrlList.id.asc())
                .limit(limit)
            )                                               # all은 엔티티들을 즉시 다 뽑아서 리스트로 만들어줌
            return list(sess.execute(stmt).scalars().all()) # scalar(s)는 튜플의 첫번째 값을 주는데, 여기서 select에 엔티티클래스를 쓰면 튜플의 첫번쨰가 엔티티자체가 됨!!

    def select_sources(self) -> list[UrlList]:
        """등록된 시작 URL (is_source == True) 전체 목록 조회"""
        with self.session() as sess:
            stmt = (
                select(UrlList)
                .where(UrlList.is_source == True)
                .order_by(UrlList.id.asc())
            )
            return list(sess.execute(stmt).scalars().all())

    def select_filtered_urls(self, limit: int = 500, after_id: int = 0) -> list[FilteredUrl]:
        """필터링을 통과한 FilteredUrl 목록 조회 (Keyset Pagination)"""
        with self.session() as sess:
            stmt = (
                select(FilteredUrl)
                .where(FilteredUrl.id > after_id)
                .order_by(FilteredUrl.id.asc())
                .limit(limit)
            )
            return list(sess.execute(stmt).scalars().all())

    def select_unextracted_filtered_urls(self, limit: int = 500, after_id: int = 0) -> list[FilteredUrl]:
        """
        아직 일정(ExtractedSchedule)이 추출되지 않은 FilteredUrl 목록 조회 (Keyset Pagination)
        ExtractedSchedule과 Outer Join하여 일정이 없는 FilteredUrl만 선별합니다.
        """
        with self.session() as sess:
            stmt = (
                select(FilteredUrl)
                .outerjoin(ExtractedSchedule, FilteredUrl.url == ExtractedSchedule.url)
                .where(ExtractedSchedule.id.is_(None), FilteredUrl.id > after_id)
                .order_by(FilteredUrl.id.asc())
                .limit(limit)
            )
            return list(sess.execute(stmt).scalars().all())

    def select_deactivated_urls(self, limit: int = 500, after_id: int = 0) -> list[UrlList]:
        """
        비활성화된 url들을 반환합니다 (Keyset Pagination)
        """
        with self.session() as sess:
            stmt = (
                select(UrlList)
                .where(UrlList.is_active == False, UrlList.id > after_id)
                .order_by(UrlList.id.asc())
                .limit(limit)
            )
            return list(sess.execute(stmt).scalars().all())

    def deactivate_urls(self, urls: list[str]) -> int:
        """수집된 urls들에서 지정한 url들을 비활성화 합니다"""
        if not urls:
            return 0
        with self.session() as sess:
            stmt = (
                update(UrlList)
                .where(UrlList.url.in_(urls))
                .values(is_active=False)
            )
            result = sess.execute(stmt)
            return result.rowcount

    def close(self) -> None:
        """엔진 연결 종료"""
        self.engine.dispose()
