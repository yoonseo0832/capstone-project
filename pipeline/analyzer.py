from __future__ import annotations

from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from ..core.locator import LocatorNode
from .html_cleaner import clean_html

if TYPE_CHECKING:
    from ..core.session import Page

__all__ = [
    "PageAnalyzer",
]


class PageAnalyzer:

    def __init__(self, page: Page):
        self.page: Page = page

    # ================= [1. 수집 / Extraction] =================
    async def get_clean_html(self) -> str:
        """iframe이 병합되고 노이즈 태그가 제거된 깨끗한 HTML을 반환합니다."""
        return clean_html(await self.page.get_raw_content())

    async def get_post_processed_content(self) -> str:
        """기존 메서드와의 호환성을 위한 헬퍼 (get_clean_html과 동일)"""
        return await self.get_clean_html()

    async def get_text(self) -> str:
        """HTML 태그를 걷어낸 본문 순수 텍스트를 반환합니다. (데이터 저장 및 LLM 문서 전달용)"""
        cleaned_html = await self.get_clean_html()
        soup = BeautifulSoup(cleaned_html, "html.parser")
        return soup.get_text(separator="\n", strip=True)

    # ================= [2. 조작 및 탐색 / Action] =================
    async def get_interactive_elements(self, alive_only: bool = True) -> list[tuple[int, int, LocatorNode]]:
        """
        크롤러나 에이전트 알고리즘이 순회할 수 있도록 
        (frame_idx, orig_locator_idx, LocatorNode) 목록을 반환합니다.
        alive_only=True면 죽은 프레임/로케이터는 제외합니다.
        """
        page_info = await self.page.get_page_info()
        elements: list[tuple[int, int, LocatorNode]] = []
        for f_idx, fi in enumerate(page_info.frameInfos):
            if alive_only and not fi.is_available:
                continue
            for orig_idx, node in enumerate(fi.locator_nodes):
                if alive_only and not node.is_alive():
                    continue
                elements.append((f_idx, orig_idx, node))
        return elements

    async def print_page_info(self, show_dead: bool = True) -> str:
        """에이전트(LLM) 프롬프트나 디버깅 로그에 넣기 위한 UI 상태 요약 문자열을 반환합니다."""
        page_info = await self.page.get_page_info()
        frame_infos = page_info.frameInfos
        result = [
            f"Page URL: {page_info.url}",
            f"Page Title: {page_info.title}",
            f"Frame Count: {len(frame_infos)}",
            f"Locator Header: {'|'.join((['index', 'alive', 'excluded'] + LocatorNode.keys()))}",
            "",
        ]
        for i, frameInfo in enumerate(frame_infos):
            frame_status = "ALIVE" if frameInfo.is_available else "DEAD"
            result_frame = [f"Frame URL: {frameInfo.url} [{frame_status}]"]
            for j, locator_node in enumerate(frameInfo.locator_nodes):
                if not show_dead and not locator_node.is_alive():
                    continue
                loc_status = "O" if locator_node.is_alive() else "X"
                loc_excluded = "O" if locator_node.is_excluded() else "X"
                line = ", ".join(locator_node.values())
                result_frame.append(f"  [{j}][{loc_status}][{loc_excluded}] {line}")
            result_frame.append("")
            result_frame_str = "\n".join(result_frame)
            result.append(f"[Frame {i}]\n{result_frame_str}")
        return "\n".join(result)
