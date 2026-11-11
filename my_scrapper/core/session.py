from __future__ import annotations

import asyncio
import difflib
import sys
import time
import uuid
from dataclasses import dataclass
from typing import override

from bs4 import BeautifulSoup
from playwright.async_api import (
    Browser,
    BrowserContext,
    Frame as PlaywrightFrame,
    Page as PlaywrightPage,
    Playwright,
    async_playwright,
)

from ..utils.urls import get_clean_url, is_same_page_url
from .commands import Click, Command, Fill
from .locator import (
    LocatorConfig,
    LocatorManager,
    LocatorNode,
    Pair,
    StabilityConfig,
)

__all__ = [
    # Core session / page classes & errors
    "Context",
    "FrameRestoreError",
    "Page",
    "Page_State",
    "Frame",
    "FrameInfo",
    "PageInfo",
    "recursive_iframe_replace",
    # Re-exported from locator
    "Pair",
    "StabilityConfig",
    "LocatorConfig",
    "LocatorNode",
    "LocatorManager",
    # Re-exported from commands
    "Command",
    "Click",
    "Fill",
]


async def recursive_iframe_replace(root: PlaywrightFrame) -> str:
    """각 iframe 태그 안에 그 프레임 자신의 내용을 넣습니다."""
    markers: dict[str, PlaywrightFrame] = {}
    for child in root.child_frames:
        if child.is_detached():
            continue
        try:
            element = await child.frame_element()  # 부모 문서 안의 <iframe> 요소
            marker = uuid.uuid4().hex
            await element.evaluate("(el, m) => el.setAttribute('data-mcp-frame', m)", marker)
            markers[marker] = child
        except Exception:
            continue  # 그 사이 사라진 프레임은 건너뜀

    content = await root.content()  # 표식이 심어진 뒤에 스냅샷
    if not markers:
        return content

    soup = BeautifulSoup(content, "html.parser")  # 부모 문서는 한 번만 파싱
    for marker, child in markers.items():
        target = soup.find(["iframe", "frame"], attrs={"data-mcp-frame": marker})
        if target is None:
            continue
        child_html = await recursive_iframe_replace(child)  # 중첩 iframe은 재귀로 처리
        target.append(BeautifulSoup(child_html, "html.parser"))
        del target["data-mcp-frame"]  # 결과물에는 표식을 남기지 않음
    return str(soup)



class Context:
    playwright: Playwright | None = None
    browser: Browser | None = None
    ref_cnt: int = 0

    @classmethod
    async def create(cls, session_path: str | None, headless: bool = False) -> "Context":
        if Context.playwright is None:
            Context.playwright = await async_playwright().start()
        if Context.browser is None:
            Context.browser = await Context.playwright.chromium.launch(
                headless=headless,
                args=[
                    "--disable-features=Translate",
                    "--disable-translate",
                ],
            )
            # Context.browser = await Context.playwright.chromium.launch(headless=True)

        # 세션 파일이 존재하나 확인
        if session_path is not None:
            try:
                with open(session_path, "rb") as f:
                    pass
            except FileNotFoundError:
                # 없으면 None으로
                session_path = None

        # context 생성
        Context.ref_cnt += 1
        assert Context.browser is not None
        context = await Context.browser.new_context(
            storage_state=session_path,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        )
        return Context(context, session_path)

    def __init__(self, context: BrowserContext, session_path: str | None):
        self.context: BrowserContext = context
        self.session_path: str | None = session_path  # 세션경로
        self.pages: list[Page] = []  # 자식페이지들

    async def new_page(self) -> "Page":
        # page객체를 반환함
        n_page = Page(await self.context.new_page())
        self.pages.append(n_page)
        return n_page

    async def reload(self, restore_pages: bool = True) -> None:
        # 세션을 새로고침
        # restore_pages: page복원여부
        await self.context.close()
        assert Context.browser is not None
        self.context = await Context.browser.new_context(
            storage_state=self.session_path,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        )
        # 복원시
        if restore_pages:
            for page in self.pages:
                await page.restore(await self.context.new_page())
        # 복원x
        else:
            self.pages = []

    async def close(self) -> None:
        await self.context.close()
        Context.ref_cnt -= 1
        if Context.ref_cnt <= 0:
            if Context.browser is not None:
                await Context.browser.close()
                Context.browser = None
            if Context.playwright is not None:
                await Context.playwright.stop()
                Context.playwright = None

    async def save_session(self) -> None:
        # 세션저장
        await self.context.storage_state(path=self.session_path)


class FrameRestoreError(Exception):
    """상태 복원 중 커맨드 재생이 실패해서, 해당 지점부터 더 이상 복원할 수 없음을 나타냄"""

    def __init__(self, message: str, failed_steps: int):
        super().__init__(message)
        self.failed_steps = failed_steps  # 재생 못 한(실패 지점부터 끝까지) pair 개수


class Page:
    # 페이지를 나타냄
    def __init__(self, page: PlaywrightPage):
        self.page: PlaywrightPage = page
        self.records: list[Page_State] = []  # 페이지 이동기록을 나타냄
        self.command_buffer: list[Command] = []  # 실행할 커맨드들

        self.page_changed: bool = False
        self.frame_changed: bool = False
        self._bind_page_events(page)

    def _bind_page_events(self, page: PlaywrightPage) -> None:
        page.on("load", self.on_real_navigation)  # 진짜 페이지 이동만
        page.on("frameattached", self.on_detach_attach)
        page.on("framedetached", self.on_detach_attach)
        page.on("framenavigated", self.on_frame_navigated)  # 서브프레임 감지용으로만 남김

    def on_real_navigation(self, page: PlaywrightPage) -> None:
        """진짜 새 문서가 로드됐을 때만 호출됨 (프래그먼트/pushState는 안 옴)"""
        self.page_changed = True

    def on_frame_navigated(self, frame: PlaywrightFrame) -> None:
        """서브프레임(iframe) 관련 감지용"""
        if frame != self.page.main_frame:
            self.frame_changed = True

    def on_detach_attach(self, frame: PlaywrightFrame) -> None:
        """프레임이 떨어지거나 붙으면 호출됩니다"""
        self.frame_changed = True

    def _get_current_state(self) -> "Page_State":
        """현재 상태를 반환"""
        return self.records[-1]

    def _append_command(self, command: Command):
        """버퍼에 나중에 실행할 명령을 추가함"""
        self.command_buffer.append(command)

    def _pop_command(self):
        """버퍼에 실행할 명령중 마지막을 제거함"""
        if len(self.command_buffer) > 0:
            self.command_buffer.pop()

    @classmethod
    async def _extract_stable_frames(cls, page: PlaywrightPage, _timeout: int, _stable_ms: int) -> list[PlaywrightFrame]:
        """
        안정된 프레임들을 추출합니다.
        """
        await page.wait_for_load_state("domcontentloaded")
        last_frames: list[Pair[int, bool]] = [Pair(id(f), False) for f in page.frames]
        refs = {f.first: f.second for f in last_frames}  # 프레임객체가 gc안되게 잡아둘 딕셔너리
        start_time = time.time()
        stable_start: float | None = None

        while True:
            gap = (time.time() - start_time) * 1000
            if gap > _timeout:
                print(f"[!] _extract_stable_frames: 시간 초과(gap={gap}) (현재 상태로 그냥 진행)", file=sys.stderr)
                break
            try:
                f_temp = page.frames
                current_frames = [id(f) for f in f_temp]
                for i in range(len(current_frames)):
                    refs[current_frames[i]] = f_temp[i]
                next_frames: list[Pair[int, bool]] = []
                sm = difflib.SequenceMatcher(a=[p.first for p in last_frames], b=current_frames, autojunk=False)
                for tag, i1, i2, j1, j2 in sm.get_opcodes():
                    if tag == "equal":
                        for i in range(i2 - i1):
                            if last_frames[i1 + i].second is True:  # 없어졌다가 다시 생긴 프레임은 제외
                                next_frames.append(last_frames[i1 + i])
                                continue
                            # 계속 있던 프레임들은 그대로 추가
                            next_frames.append(Pair(current_frames[j1 + i], False))
                        continue
                    if tag == "delete":
                        for ln in last_frames[i1:i2]:
                            ln.second = True  # 없어졌다는 표시...
                        next_frames += last_frames[i1:i2]
                        continue
                    if tag == "insert":  # 처음보는경우(생성된 경우는 일단 추가)
                        next_frames += [Pair(n, False) for n in current_frames[j1:j2]]
                        continue
                    if tag == "replace":
                        next_frames += [Pair(n, False) for n in current_frames[j1:j2]]  # 새로생긴거는 추가
                        for ln in last_frames[i1:i2]:  # 없어진거는 없어진거를 표시
                            ln.second = True  # 없어졌다는 표시...
                        next_frames += last_frames[i1:i2]
                        continue

                if next_frames == last_frames:
                    if stable_start is None:
                        stable_start = time.time()
                    # 2. 지정된 시간(예: 500ms) 동안 변화가 없었다면 조건 충족
                    if (time.time() - stable_start) * 1000 >= _stable_ms:
                        break  # 완전히 안정화됨
                else:
                    # 변화가 생겼다면 타이머를 초기화하고 최신 상태를 기록
                    stable_start = None
                    last_frames = next_frames
            except Exception:
                await asyncio.sleep(0.2)
                continue
            await asyncio.sleep(0.1)
        return [refs[i.first] for i in last_frames if not i.second]

    @classmethod
    def _filter_unidentifiable_frames(cls, stable_frames: list[PlaywrightFrame]) -> list[PlaywrightFrame]:
        """url이 고유한 프레임만 추출함"""
        cnt = {}
        for stable_frame in stable_frames:
            cur_frame_url = get_clean_url(stable_frame.url)
            if cnt.get(cur_frame_url) is None:
                cnt[cur_frame_url] = 0
            cnt[cur_frame_url] += 1
        ret = []
        for stable_frame in stable_frames:
            cur_frame_url = get_clean_url(stable_frame.url)
            cur_cnt = cnt.get(cur_frame_url)
            if cur_cnt is None or cur_cnt > 1:
                continue
            ret.append(stable_frame)
        return ret

    async def _goto_page_state(self, record_idx: int | None, state_idx: int | None):
        """record내 특정 페이지 상태로 이동합니다, 이때 명령버퍼는 초기화 됩니다."""
        # 명령버퍼 초기화
        self.command_buffer = []
        # idx범위검사
        if record_idx is None:  # None이면 그냥 마지막으로 초기화
            record_idx = len(self.records) - 1
        if record_idx < 0 or record_idx >= len(self.records):
            raise ValueError(f"given record_idx is out of range: given:{record_idx}, available_range:{0}~{len(self.records)}")
        if state_idx is None:  # None이면 그냥 마지막으로 초기화
            state_idx = len(self.records[record_idx].command_frames) - 1
        if state_idx < 0 or state_idx >= self.records[record_idx].len_commands:
            raise ValueError(f"given state_idx is out of range: given:{state_idx}, available_range:{0}~{self.records[record_idx].len_commands}")
        # idx범위로 자름
        self.records = self.records[: record_idx + 1]
        cur_state = self._get_current_state()
        cur_state.cut_at_idx(state_idx)
        cur_state.get_current_state().first = None  # 마지막에 있던 command는 실행안되게!
        # 상태복원
        await self.page.goto(cur_state.page_url)  # 해당 페이지 상태의 url로 이동
        configs = StabilityConfig()
        total_pairs = len(cur_state.command_frames)
        for i, pair in enumerate(cur_state.command_frames):
            # 현재프레임들 상태 복구
            stable_frames = await Page._extract_stable_frames(self.page, configs.timeout, configs.stable_ms)
            stable_frames = Page._filter_unidentifiable_frames(stable_frames)
            new_frames_dict = {get_clean_url(f.url): f for f in stable_frames}
            for cur_frame in pair.second:
                await cur_frame.restore(new_frames_dict.get(cur_frame.init_url))
            if pair.first is not None:
                # 복구후 커맨드 실행
                try:
                    await pair.first.do(pair.second)
                except Exception as e:  # 끝까지 복원 실패한경우
                    failed_action = pair.first.action
                    # 실제로 도달한 건 딱 이 pair(i번째)까지이므로, 기록도 거기까지로 되돌림(현재 복원한데 까지만 남김)
                    cur_state.command_frames = cur_state.command_frames[: i + 1]
                    cur_state.command_frames[-1].first = None  # 여기서 더 못 나간다는 표시
                    failed_steps = total_pairs - i - 1  # i번째(0-indexed)부터 끝까지 못 감 -> 실패 개수
                    raise FrameRestoreError(
                        f"복원 중 커맨드 재생 실패 (i={i}/{total_pairs}): {failed_action} ({e})",
                        failed_steps=failed_steps,
                    ) from e

    async def _apply_commands_in_buffer(self) -> bool:
        """뭔가 변화가 있으면(페이지,로케이터) True반환"""

        configs = StabilityConfig()
        current_state = self._get_current_state()

        command_buffer = self.command_buffer
        self.command_buffer = []
        self.page_changed = False
        self.frame_changed = False

        if len(command_buffer) <= 0:
            return False

        for command in command_buffer:
            current_frames = current_state.get_current_state().second
            try:
                await command.do(current_frames)
            except Exception as e:
                if is_same_page_url(self.page.url, current_state.page_url):
                    print(f"page:알수없는 이유로 커맨드 실행이 실패함, 실패한 로케이터를 표시하고 건너뜀\ne:{e}")
                    f_idxs, l_idxs = command.frame_idxs, command.locator_idxs  # 제외할 것들(여러개가 있을때, 앞에꺼 하나 실패했는데도 뒤에것도 다 제외하는 문제 발생가능..)
                    for f_idx, l_idx in zip(f_idxs, l_idxs):
                        current_frames[f_idx].locator_manager.exclude_locators([l_idx])
                    continue

            # 성공했든, 예외났지만 네비게이션됐든 공통으로 결과 반영
            stable_frames = await Page._extract_stable_frames(self.page, configs.timeout, configs.stable_ms)
            stable_frames = Page._filter_unidentifiable_frames(stable_frames)

            # 1.페이지가 이동되었나 확인
            if self.page_changed:
                print("page:페이지 이동됨")
                current_state.command_frames[-1].first = command  # 해당프레임에 어떤 커맨드를 적용했는지 저장
                new_state = Page_State(
                    [await Frame.create(frame) for frame in stable_frames],
                    None,  # 아직 아무커맨드도 적용안함(새거)
                    self.page.url,
                    await self.page.title(),
                )
                self.records.append(new_state)
                return True  # 성공했으므로(상태가 바뀜) 나머지는 무효화

            # 2.프레임변화 확인
            if self.frame_changed:
                last_state = current_state.get_current_state()
                last_url_set = {f.init_url for f in last_state.second}
                new_set = {get_clean_url(f.url) for f in stable_frames}
                if last_url_set != new_set:
                    print("page:프레임 변화함")
                    last_state.first = command  # 해당 프레임에서 어떤 커맨드 실행했는지 저장
                    current_state.append_state(
                        None,
                        [await Frame.create(frame) for frame in stable_frames],
                    )
                    return True  # 성공했으므로(프레임이 바뀜) 나머지는 무효화

            # 3.로케이터변화 확인
            new_frames = [await Frame.create(f) for f in stable_frames]  # 여기서 개선이 필요함... create시에 매번 클릭가능한거를 필터링하는데, 너무 오래걸림

            last_frames_dict = {f.init_url: f for f in current_state.get_current_state().second}
            for new_frame in new_frames:
                last_frame = last_frames_dict[new_frame.init_url]
                if new_frame != last_frame:
                    print("page:로케이터 변화함")
                    # print(f"new/last: {new_frame}/{last_frame}")
                    last_state = current_state.get_current_state()
                    last_state.first = command  # 해당 프레임에서 어떤 커맨드 실행했는지 저장
                    current_state.append_state(
                        None,
                        new_frames,
                    )
                    return True  # 성공했으므로(프레임이 바뀜) 나머지는 무효화
            # 내용상 "변화 없음"으로 결론나도, 핸들은 최신으로 갱신해줘야 stale 방지(그냥 떨어졌다가 다시 붙는경우)
            new_frames_dict = {get_clean_url(f.url): f for f in stable_frames}
            for cur_frame in current_frames:
                await cur_frame.restore(new_frames_dict.get(cur_frame.init_url))
        # 루프가 끝나면 변화없음(모든 커맨드 적용됨)
        print("page:변화없음")
        return False

    async def reset_records(self):
        self.records = []

    async def goto(self, url: str) -> None:
        # 페이지 이동
        await self.page.goto(url, wait_until="domcontentloaded")
        configs = StabilityConfig()
        # 프레임 대기
        stable_frames = await Page._extract_stable_frames(self.page, configs.timeout, configs.stable_ms)
        stable_frames = Page._filter_unidentifiable_frames(stable_frames)
        # self.records = []  # 기록 초기화
        self.records.append(
            Page_State(
                [await Frame.create(frame) for frame in stable_frames],
                None,
                self.page.url,
                await self.page.title(),
            )
        )

    async def click_locator(self, frame_idx: int, locator_idx: int) -> bool:
        self._append_command(Click(frame_idx, [locator_idx]))
        return await self._apply_commands_in_buffer()

    async def fill_locators(
        self,
        frame_idxs: list[int],
        locator_idxs: list[int],
        contents: list[str],
        last_is_submit: bool = False,
    ) -> bool:
        require_contents_len = len(locator_idxs) - int(last_is_submit)
        if require_contents_len != len(contents):
            raise ValueError(
                f"length of [locator_idxs] and [contents] should be same: currently {require_contents_len}|{len(contents)}"
            )
        self._append_command(Fill(frame_idxs, locator_idxs, last_is_submit=last_is_submit, contents=contents))
        return await self._apply_commands_in_buffer()

    async def get_page_info(self) -> "PageInfo":
        if not self.records:
            return PageInfo("", "", [])
        record = self._get_current_state()
        return PageInfo(
            record.page_url,
            await self.page.title(),
            [frame.get_frame_info() for frame in record.get_current_state().second],
        )

    async def get_raw_content(self) -> str:
        return await recursive_iframe_replace(self.page.main_frame)

    async def undo(self):
        dest_record_idx = len(self.records) - 1
        dest_state_idx = len(self._get_current_state().command_frames) - 2
        if dest_state_idx < 0:
            dest_record_idx -= 1
            dest_state_idx = None
        await self._goto_page_state(dest_record_idx, dest_state_idx)

    async def restore(self, page: PlaywrightPage):
        """새로운 페이지 객체로 교체후, 상태를 복구함"""
        self.page = page
        self._bind_page_events(page)
        await self._goto_page_state(None, None)


class Page_State:

    def __init__(self, init_frames: list["Frame"], init_command: Command | None, page_url: str, page_title: str):
        self.page_url: str = get_clean_url(page_url) # '#'떼고 저장
        self.page_title: str = page_title
        self.command_frames: list[Pair[Command | None, list[Frame]]] = [Pair(init_command, init_frames)]  # 첫번째는 command, 두번째는 해당커맨드 했을때 프레임들

    def append_state(self, command: Command | None, frames: list["Frame"]) -> None:
        """커맨드와 해당커맨드에대한 결과상태를 추가함"""
        self.command_frames.append(Pair(command, frames))

    def pop_last_state(self):
        """마지막 상태를 제거함"""
        if self.len_commands <= 1:
            raise Exception("제거할 상태가 없습니다.")
        self.command_frames.pop()

    def get_current_state(self) -> Pair[Command | None, list[Frame]]:
        """현재(마지막) 상태를 반환"""
        return self.command_frames[-1]

    def cut_at_idx(self, idx: int):
        """command_idx까지 남기고 잘라냄, 뒷부분 버림"""
        if self.len_commands <= idx:
            raise Exception("주어진 idx가 현재 최대 인덱스보다 큽니다.")
        self.command_frames = self.command_frames[: idx + 1]

    @property
    def len_commands(self) -> int:
        return len(self.command_frames)


class Frame:
    # iframe+메인프레임을 나타냄, 상태를 저장하고 있다가, frame을 받으면 복구함
    @classmethod
    async def create(cls, frame: PlaywrightFrame) -> "Frame":
        # 초기화
        locator_manager = await LocatorManager.create(frame, LocatorConfig(), StabilityConfig())
        return Frame(get_clean_url(frame.url), locator_manager)

    def __init__(self, init_url: str, locator_manager: LocatorManager):
        self.locator_manager: LocatorManager = locator_manager
        self.init_url: str = init_url  # 프레임의 주소
        self.is_available = True

    def is_alive(self) -> bool:
        return self.is_available

    def get_init_url(self) -> str:
        return self.init_url

    @override
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Frame):
            return NotImplemented
        return self.locator_manager == other.locator_manager and self.init_url == other.init_url

    async def restore(self, frame: PlaywrightFrame | None) -> None:
        """입력받은 frame으로 갱신함"""
        if frame is None:  # 없다면
            self.is_available = False
            await self.locator_manager.restore(None)
            return
        await self.locator_manager.restore(frame)
        self.is_available = True

    def exclude_locators(self, l_idxs: list[int]) -> None:
        self.locator_manager.exclude_locators(l_idxs)

    def get_frame_info(self) -> "FrameInfo":
        return FrameInfo(self.init_url, self.is_available, self.locator_manager.locator_nodes or [])  # 마지막 로케이터들 반환


@dataclass
class FrameInfo:
    # 프레임의 정보를 나타냄
    url: str
    is_available: bool
    locator_nodes: list[LocatorNode]


@dataclass
class PageInfo:
    # 페이지의 정보를 나타냄
    url: str
    title: str
    frameInfos: list[FrameInfo]
