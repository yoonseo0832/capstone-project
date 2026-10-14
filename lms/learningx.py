"""LearningX(Xinics) 'Board' 외부 LTI 앱에서 게시판 공지를 수집.

Canvas 기본 공지(Announcements)에는 없고 Board 탭에만 올라오는 공지
(예: 중간/기말고사 안내)를 가져오기 위한 연동이다.

흐름:
  Canvas sessionless LTI 런치  →  LearningX 세션(xn_api_token 쿠키)  →  LearningX API
    GET courses/{id}/tabs                      : 'Board' 탭의 외부도구 id 찾기
    GET courses/{id}/external_tools/sessionless_launch?id=<tool>  : 서명된 런치 폼
    POST <learningx_board/boards>              : 세션 수립(쿠키 발급)
    GET /learningx/api/v1/learningx_board/courses/{id}/boards     : 게시판 목록(+본문 description)

주의: Canvas 공식 API 가 아니라 LearningX 의 비공식 엔드포인트라, LMS 업데이트 시
깨질 수 있다. 실패는 상위(sync)에서 과제/공지 수집과 독립적으로 삼켜서 처리한다.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from lms.canvas_client import REQUEST_TIMEOUT, html_to_text_and_links

log = logging.getLogger(__name__)

API_PREFIX = "/learningx/api/v1"
BOARD_TOOL_LABEL = "Board"
TOOL_ID_RE = re.compile(r"[?&]id=(\d+)")
# 공지 글이 들어있을 법한 게시판(제목에 공지/announcement/notice) → 안의 글도 가져온다
ANNOUNCE_RE = re.compile(r"announce|공지|notice", re.I)


def _is_notice_board(b: dict) -> bool:
    """공지/안내 성격의 게시판만 고른다.
    - type=='notice' : 공지 게시판
    - from_module & type!='qna' : 모듈(공지)에서 자동 생성된 안내 게시판 (Final exam 등)
    제외: Q&A 게시판, 강의자료실(수동 normal), 프로젝트 토론(qna) 등."""
    return b.get("type") == "notice" or (bool(b.get("from_module")) and b.get("type") != "qna")


def _item(source_id, title, html, course_id, course_name, base_url, tool_id):
    """공통 저장용 dict 생성 (게시판/게시글 공용)."""
    text, links = html_to_text_and_links(html, base_url)
    if not text.strip():
        text = title or ""        # 본문이 없으면 제목이 곧 공지 (예: 'Final exam on Thursday 9AM')
    return {
        "source_id": source_id,
        "item_type": "lx_board",
        "course_id": course_id,
        "course_name": course_name,
        "title": title or "(제목 없음)",
        "content": text,
        "content_html": html or "",
        # Canvas 안에서 Board 앱을 여는 링크 (로그인 상태면 바로 열림)
        "link": f"{base_url}/courses/{course_id}/external_tools/{tool_id}",
        "extracted_links": links,
        "due_at": None,
        "start_at": None,         # raw_items 에 start_at 컬럼이 없어 저장에는 안 쓰임
        "submitted": False,
    }


def _is_announcement_container(b: dict) -> bool:
    """안에 공지 '글'이 들어있을 법한 게시판인지 (빅데이터개론처럼 글로 공지하는 방식)."""
    return b.get("type") == "notice" or bool(ANNOUNCE_RE.search(b.get("title") or ""))


def normalize_board(b: dict, course_id: int, course_name: str, base_url: str, tool_id: int) -> dict | None:
    """게시판 자체(제목+본문)를 공지로. 공지성 게시판이 아니면 None (정보통신처럼 게시판=공지인 방식)."""
    if not _is_notice_board(b):
        return None
    return _item(f"lxboard:{b['id']}", b.get("title"), b.get("description"),
                 course_id, course_name, base_url, tool_id)


def normalize_post(p: dict, course_id: int, course_name: str, base_url: str, tool_id: int) -> dict:
    """게시판 안의 글 1개를 공지로 (빅데이터개론처럼 'Announcement' 게시판에 글로 공지하는 방식)."""
    return _item(f"lxpost:{p['id']}", p.get("title"), p.get("content") or p.get("body"),
                 course_id, course_name, base_url, tool_id)


class LearningXClient:
    """CanvasClient 의 인증된 requests 세션을 재사용해 LearningX Board 를 읽는다."""

    def __init__(self, canvas_client, base_url: str | None = None):
        self._req = canvas_client._canvas._Canvas__requester
        self.session = self._req._session
        self.base = (base_url or canvas_client.base_url).rstrip("/")

    def board_tool_id(self, course_id: int) -> int | None:
        """과목 탭에서 'Board' 외부도구 id 를 찾는다. 없으면 None(Board 탭 없는 과목)."""
        for tab in self._req.request("GET", f"courses/{course_id}/tabs").json():
            if tab.get("label") == BOARD_TOOL_LABEL and tab.get("type") == "external":
                m = TOOL_ID_RE.search(tab.get("url", ""))
                if m:
                    return int(m.group(1))
        return None

    def _session_token(self, course_id: int, tool_id: int) -> str:
        """LTI 런치를 수행해 LearningX 세션 토큰(xn_api_token)을 얻는다."""
        launch = self._req.request(
            "GET", f"courses/{course_id}/external_tools/sessionless_launch",
            id=tool_id, launch_type="course_navigation").json()
        page = self.session.get(launch["url"], timeout=REQUEST_TIMEOUT).text
        form = BeautifulSoup(page, "html.parser").find("form")
        if form is None or not form.get("action"):
            raise RuntimeError("LearningX LTI 런치 폼을 찾지 못했습니다.")
        data = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}
        self.session.post(form["action"], data=data, timeout=REQUEST_TIMEOUT)
        token = self.session.cookies.get("xn_api_token")
        if not token:
            raise RuntimeError("LearningX 세션 토큰(xn_api_token)을 받지 못했습니다.")
        return token

    def _get(self, token: str, path: str, **params) -> list[dict]:
        r = self.session.get(f"{self.base}{API_PREFIX}{path}", timeout=REQUEST_TIMEOUT, params=params,
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
        return data.get("items", []) if isinstance(data, dict) else (data or [])

    def _boards(self, course_id: int, token: str) -> list[dict]:
        return self._get(token, f"/learningx_board/courses/{course_id}/boards")

    def _posts(self, course_id: int, board_id: int, token: str) -> list[dict]:
        return self._get(token, f"/learningx_board/courses/{course_id}/boards/{board_id}/posts", per_page=100)

    def iter_notices(self, course: dict):
        """과목의 Board 공지를 저장용 dict 로 하나씩 내보낸다.

        두 가지 방식 모두 지원:
        - 게시판 자체가 공지 (정보통신): 게시판 제목+본문을 공지로
        - 'Announcement' 게시판에 글로 공지 (빅데이터개론): 게시판 안의 글을 공지로
        """
        cid = course["id"]
        tool_id = self.board_tool_id(cid)
        if tool_id is None:
            log.info("강의 %s: Board 탭 없음 → LearningX 수집 건너뜀", course.get("name"))
            return
        token = self._session_token(cid, tool_id)
        for b in self._boards(cid, token):
            item = normalize_board(b, cid, course["name"], self.base, tool_id)
            if item is not None:
                yield item
            # 공지 글이 들어있는 게시판이면 그 안의 글도 공지로 수집
            if _is_announcement_container(b) and (b.get("total_post_count") or 0) > 0:
                for p in self._posts(cid, b["id"], token):
                    yield normalize_post(p, cid, course["name"], self.base, tool_id)
