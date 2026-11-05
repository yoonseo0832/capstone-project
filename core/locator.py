from __future__ import annotations

import asyncio
import difflib
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar, override

from playwright.async_api import Frame as PlaywrightFrame, Locator as PlaywrightLocator

__all__ = [
    "Pair",
    "StabilityConfig",
    "LocatorConfig",
    "LocatorNode",
    "LocatorManager",
]

T1 = TypeVar("T1")
T2 = TypeVar("T2")


@dataclass
class Pair(Generic[T1, T2]):
    first: T1
    second: T2


@dataclass
class StabilityConfig:
    timeout: int = 5000
    stable_ms: int = 1000
    interval: float = 0.2


@dataclass
class LocatorConfig:
    selector_include: list[str] = field(
        default_factory=lambda: ["a", "li", "span", "div", "p", "button", "input", "textarea", "select"]
    )
    keyword_forbidden: list[str] = field(
        default_factory=lambda: [
            "로그아웃", "logout", "signout","login","로그인",
            "exit", "나가기",
            "비밀번호 변경", 
            "회원탈퇴", "delete account", 
            "language", "언어"
            ]
    )


class LocatorNode:

    def __init__(self, tag: str, text: str, value: str, placeholder: str, href: str, locator: PlaywrightLocator):
        self.tag: str = tag
        self.text: str = text
        self.value: str = value
        self.placeholder: str = placeholder
        self.href: str = href
        self.locator: PlaywrightLocator = locator
        self._is_available: bool = True
        self._is_excluded: bool = False  # 커맨드 실패기록이 있는지

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, LocatorNode):
            return NotImplemented
        return self.values() == other.values()

    @override
    def __hash__(self) -> int:
        return hash(str(self.values()))

    @classmethod
    async def create(cls, locator: PlaywrightLocator) -> "LocatorNode":
        infos = await locator.evaluate(
            """
            el => ({
                tag: el.tagName,
                text: (el.innerText || "").trim(),
                value: el.value || "",
                placeholder: el.placeholder || "",
                href: el.href || ""
            })
            """,
            timeout=2000,
        )
        return LocatorNode(**infos, locator=locator)

    async def click(self) -> None:
        await self.locator.click(timeout=3000)

    async def fill(self, content: str) -> None:
        await self.locator.fill(content)

    def is_alive(self) -> bool:
        return self._is_available

    def is_excluded(self) -> bool:
        return self._is_excluded

    def exclude(self) -> None:
        self._is_excluded = True

    def restore(self, locator: PlaywrightLocator | None):
        if locator is None:
            self._is_available = False
        else:
            self.locator = locator
            self._is_available = True

    @classmethod
    def keys(cls) -> list[str]:
        return ["tag", "text", "value", "placeholder", "href"]

    def values(self) -> list[str]:
        return [self.tag, self.text, self.value, self.placeholder, self.href]


class LocatorManager:

    def __init__(
        self,
        stable_nodes: list[LocatorNode],
        locator_nodes: list[LocatorNode],
        selector_include: list[str],
        keyword_forbidden: list[str],
        timeout: int,
        stable_ms: int,
        interval: float,
    ):
        self._selector_include: list[str] = selector_include
        self._keyword_forbidden: list[str] = keyword_forbidden
        self.locator_nodes: list[LocatorNode] = locator_nodes
        self._stable_nodes: list[LocatorNode] = stable_nodes
        self._timeout: int = timeout
        self._stable_ms: int = stable_ms
        self._interval: float = interval

    @classmethod
    async def _extract(cls, frame: PlaywrightFrame, _selector_include: list[str], _keyword_forbidden: list[str]) -> list[PlaywrightLocator]:
        """프레임에서 후보 요소들을 뽑아줍니다."""
        selector_str = ", ".join(_selector_include)
        await frame.evaluate(
            """
            (args) => {
                const { sel, keywords } = args;
                const elements = document.querySelectorAll(sel);
                elements.forEach(el => {
                    const text =
                        (el.innerText || "").toLowerCase();
                    const href =
                        (el.getAttribute("href") || "")
                        .toLowerCase();
                    const isForbidden =
                        keywords.some(k =>
                            text.includes(k) ||
                            href.includes(k)
                        );
                    if (isForbidden)
                        return;
                    const hasText = Array.from(el.childNodes).some(node =>
                        node.nodeType === 3 && node.textContent.trim().length > 0
                    );
                    const isInteractive =
                        el.matches(
                            "button, input, textarea, select"
                        );
                    if (hasText || isInteractive) {
                        el.classList.add(
                            "mcp-clickable-target"
                        );
                    }
                });
            }
            """,
            {
                "sel": selector_str,
                "keywords": _keyword_forbidden,
            },
        )
        return (
            await frame
            .locator(".mcp-clickable-target")
            .filter(visible=True)
            .all()
        )

    @classmethod
    async def _extract_stable(
        cls,
        frame: PlaywrightFrame,
        _timeout: int,
        _stable_ms: int,
        _selector_include: list[str],
        _keyword_forbidden: list[str],
    ) -> list[PlaywrightLocator]:
        last_locator_nodes: list[Pair[LocatorNode, bool]] = []
        stable_start: float | None = None

        await frame.wait_for_load_state("domcontentloaded")
        last_locator_nodes = [Pair(await LocatorNode.create(l), False) for l in await cls._extract(frame, _selector_include, _keyword_forbidden)]
        start_time = time.time()

        while True:
            gap = (time.time() - start_time) * 1000
            if gap > _timeout:
                print(f"[!] _extract_stable: 시간 초과(gap={gap}) (현재 상태로 그냥 진행)", file=sys.stderr)
                break
            try:
                current_locator_nodes = [await LocatorNode.create(l) for l in await cls._extract(frame, _selector_include, _keyword_forbidden)]
                next_locator_nodes: list[Pair[LocatorNode, bool]] = []
                sm = difflib.SequenceMatcher(a=[p.first for p in last_locator_nodes], b=current_locator_nodes, autojunk=False)
                for tag, i1, i2, j1, j2 in sm.get_opcodes():
                    if tag == "equal":
                        for i in range(i2 - i1):
                            if last_locator_nodes[i1 + i].second is True:  # 없어졌다가 다시 생긴 노드는 제외
                                next_locator_nodes.append(last_locator_nodes[i1 + i])
                                continue
                            # 계속 있던 노드들은 그대로 추가
                            next_locator_nodes.append(Pair(current_locator_nodes[j1 + i], False))
                        continue
                    if tag == "delete":
                        for ln in last_locator_nodes[i1:i2]:
                            ln.second = True  # 없어졌다는 표시...
                        next_locator_nodes += last_locator_nodes[i1:i2]
                        continue
                    if tag == "insert":  # 처음보는경우(생성된 경우는 일단 추가)
                        next_locator_nodes += [Pair(n, False) for n in current_locator_nodes[j1:j2]]
                        continue
                    if tag == "replace":
                        next_locator_nodes += [Pair(n, False) for n in current_locator_nodes[j1:j2]]  # 새로생긴거는 추가
                        for ln in last_locator_nodes[i1:i2]:  # 없어진거는 없어진거를 표시
                            ln.second = True  # 없어졌다는 표시...
                        next_locator_nodes += last_locator_nodes[i1:i2]
                        continue

                if next_locator_nodes == last_locator_nodes:
                    if stable_start is None:
                        stable_start = time.time()
                    # 2. 지정된 시간(예: 500ms) 동안 변화가 없었다면 조건 충족
                    if (time.time() - stable_start) * 1000 >= _stable_ms:
                        break  # 완전히 안정화됨
                else:
                    # 변화가 생겼다면 타이머를 초기화하고 최신 상태를 기록
                    stable_start = None
                    last_locator_nodes = next_locator_nodes
            except Exception:
                await asyncio.sleep(0.2)
                continue
            await asyncio.sleep(0.1)
        return [i.first.locator for i in last_locator_nodes if not i.second]

    @classmethod
    async def _filter_clickable(cls, locator_nodes: list[LocatorNode]) -> list[LocatorNode]:
        """html에 존재하는 요소중 실제 클릭가능한 요소만 필터링 합니다"""
        clickable: list[LocatorNode] = []
        for l in locator_nodes:
            try:
                await l.locator.click(trial=True, timeout=300)  # 실제 클릭가능한지 시도해봄
            except Exception:
                continue
            clickable.append(l)
        return clickable

    @classmethod
    async def _locators_to_locatorNodes(cls, locators: list[PlaywrightLocator]) -> list[LocatorNode]:
        nodes: list[LocatorNode] = []
        for l in locators:
            try:
                nodes.append(await LocatorNode.create(l))
            except Exception:
                continue  # 그 사이 사라진/밀린 요소는 건너뜀
        return nodes

    async def restore(self, frame: PlaywrightFrame | None) -> None:
        """입력받은 프레임으로 갱신함!"""
        if frame is None:  # 전부 죽음 처리
            for locator_node in self.locator_nodes:
                locator_node.restore(None)
            return
        # 입력프레임에서 stable 로케이터들 추출
        new_stable_locators = await self._extract_stable(
            frame, self._timeout, self._stable_ms, self._selector_include, self._keyword_forbidden
        )
        new_stable_locator_nodes = await self._locators_to_locatorNodes(new_stable_locators)

        # 같은 시그니처의 로케이터가 여러개 있으면 마지막꺼만 남아버림...
        new_map = {new_locator_node: new_locator_node.locator for new_locator_node in new_stable_locator_nodes}  # 기존 로케이터들
        # 전체로케이터 갱신
        for locator_node in self.locator_nodes:
            locator_node.restore(new_map.get(locator_node))

    def exclude_locators(self, l_idxs: list[int]) -> None:
        for l_idx in l_idxs:
            self.locator_nodes[l_idx].exclude()

    @override
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LocatorManager):
            return NotImplemented
        self_set = frozenset(self.locator_nodes)
        other_set = frozenset(other.locator_nodes)
        return self_set == other_set

    @classmethod
    async def create(
        cls,
        frame: PlaywrightFrame,
        locator_configs: LocatorConfig,
        stable_configs: StabilityConfig,
    ) -> "LocatorManager":
        """LocatorManager을 생성,초기화"""
        stable_nodes = await cls._extract_stable(
            frame,
            stable_configs.timeout,
            stable_configs.stable_ms,
            locator_configs.selector_include,
            locator_configs.keyword_forbidden,
        )
        stable_nodes = await cls._locators_to_locatorNodes(stable_nodes)
        clickable_nodes = await cls._filter_clickable(stable_nodes)

        return LocatorManager(
            stable_nodes,
            clickable_nodes,
            locator_configs.selector_include,
            locator_configs.keyword_forbidden,
            stable_configs.timeout,
            stable_configs.stable_ms,
            stable_configs.interval,
        )
