"""Canvas LMS API 호출 → 저장하기 쉬운 dict 로 정규화."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable, Iterator
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from canvasapi import Canvas
from canvasapi.exceptions import CanvasException

log = logging.getLogger(__name__)

SUBMITTED_STATES = {"submitted", "graded", "pending_review"}


def to_local(iso: str | None, tz: str) -> str | None:
    """Canvas 의 UTC ISO8601 ('2026-10-10T14:59:59Z') → 로컬 'YYYY-MM-DD HH:MM'."""
    if not iso:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def html_to_text_and_links(html: str | None, base_url: str) -> tuple[str, list[str]]:
    if not html:
        return "", []
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for tag in soup.find_all(["a", "iframe"]):
        href = tag.get("href") or tag.get("src")
        if href and not href.startswith(("#", "mailto:", "javascript:")):
            full = urljoin(base_url + "/", href)
            if full not in links:
                links.append(full)
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    return text, links


def is_submitted(submission: dict | None) -> bool:
    if not submission:
        return False
    return bool(submission.get("submitted_at")) or submission.get("workflow_state") in SUBMITTED_STATES


def normalize_assignment(a: dict, course_id: int, course_name: str, base_url: str, tz: str) -> dict:
    content, links = html_to_text_and_links(a.get("description"), base_url)
    return {
        "source_id": f"assignment:{a['id']}",
        "item_type": "assignment",
        "course_id": course_id,
        "course_name": course_name,
        "title": a.get("name") or "(제목 없음)",
        "content": content,
        "content_html": a.get("description") or "",
        "link": a.get("html_url"),
        "extracted_links": links,
        "due_at": to_local(a.get("due_at"), tz),
        "start_at": to_local(a.get("unlock_at"), tz),
        "submitted": is_submitted(a.get("submission")),
    }


class CanvasClient:
    def __init__(self, base_url: str, api_key: str, tz: str = "Asia/Seoul"):
        self.base_url = base_url.rstrip("/")
        self.tz = tz
        self._canvas = Canvas(self.base_url, api_key)

    def course_url(self, course_id: int) -> str:
        return f"{self.base_url}/courses/{course_id}"

    def get_courses(self, extra_course_ids: Iterable[int] = ()) -> list[dict]:
        """수강 중(active)인 강의 + 사용자가 URL 로 추가한 강의."""
        courses: dict[int, dict] = {}
        for c in self._canvas.get_courses(enrollment_state="active"):
            if getattr(c, "name", None):  # 접근 제한된 강의는 name 이 없음
                courses[c.id] = {"id": c.id, "name": c.name}
        for cid in extra_course_ids:
            if cid in courses:
                continue
            try:
                c = self._canvas.get_course(cid)
                courses[c.id] = {"id": c.id, "name": getattr(c, "name", str(cid))}
            except CanvasException as e:
                log.warning("강의 %s 조회 실패: %s", cid, e)
        return list(courses.values())

    def iter_assignments(self, course: dict) -> Iterator[dict]:
        c = self._canvas.get_course(course["id"])
        for a in c.get_assignments(include=["submission"]):
            raw = {k: v for k, v in vars(a).items() if not k.startswith("_") and k != "requester"}
            yield normalize_assignment(raw, course["id"], course["name"], self.base_url, self.tz)
