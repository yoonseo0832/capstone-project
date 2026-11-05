from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, override

from .locator import LocatorNode

if TYPE_CHECKING:
    from .session import Frame

__all__ = [
    "Command",
    "Click",
    "Fill",
]


class Command(ABC):

    def __init__(self, frame_idxs: int | list[int], locator_idxs: int | list[int], **kwargs: Any):
        """
        frame_idxs: 각 locator가 위치한 프레임의 인덱스
        locator_idxs: 각 locator의 프레임내 인덱스
        """
        super().__init__()
        if isinstance(locator_idxs, int):
            locator_idxs = [locator_idxs]
        if isinstance(frame_idxs, int):
            frame_idxs = [frame_idxs]
        self.frame_idxs: list[int] = frame_idxs
        self.locator_idxs: list[int] = locator_idxs
        self.kwargs: dict[str, Any] = kwargs
        self.action: str | None = None

    @abstractmethod
    async def _do(self, target: LocatorNode) -> None:
        pass

    @abstractmethod
    async def _check(self) -> bool:
        pass

    async def do(self, frames: list[Frame]) -> None:
        if not await self._check():
            raise Exception(f"check fail at command {self.action}")
        for f_idx, l_idx in zip(self.frame_idxs, self.locator_idxs):
            if f_idx >= len(frames):
                raise IndexError(f"Click: frame_idx {f_idx}가 범위를 벗어남 (frames 길이:{len(frames)})")
            frame = frames[f_idx]
            nodes = frame.locator_manager.locator_nodes or []
            if l_idx >= len(nodes):
                raise IndexError(f"Click: locator_idx {l_idx}가 범위를 벗어남 (nodes 길이:{len(nodes)})")
            # 유효한 프레임인지 확인
            if not frame.is_alive():
                raise Exception(f"frame {f_idx} is not alive")
            # 유효한 로케이터인지 확인
            if not nodes[l_idx].is_alive():
                raise Exception(f"frame {f_idx} locator {l_idx} is not alive")
            # 제외된 로케이터라면 그냥 무시
            if nodes[l_idx].is_excluded():
                return
            await self._do(nodes[l_idx])


class Click(Command):

    def __init__(self, frame_idxs: int | list[int], locator_idxs: int | list[int], **kwargs: Any):
        super().__init__(frame_idxs, locator_idxs, **kwargs)
        self.action = "click"

    @override
    async def _check(self) -> bool:
        if len(self.locator_idxs) != len(self.frame_idxs):
            return False
        if len(self.locator_idxs) > 1 or len(self.frame_idxs) > 1:
            return False
        return True

    @override
    async def _do(self, target: LocatorNode) -> None:
        await target.click()


class Fill(Command):

    def __init__(self, frame_idxs: int | list[int], locator_idxs: int | list[int], **kwargs: Any):
        super().__init__(frame_idxs, locator_idxs, **kwargs)
        self.action = "fill"
        self.cur_idx = 0

    @override
    async def _check(self) -> bool:
        if len(self.locator_idxs) != len(self.frame_idxs):
            return False
        return True

    @override
    async def _do(self, target: LocatorNode) -> None:
        # fill또는 제출버튼 click중 선택
        cur_job = lambda: target.fill(self.kwargs["contents"][self.cur_idx])
        if self.kwargs.get("last_is_submit"):
            if self.cur_idx == len(self.locator_idxs) - 1:
                cur_job = lambda: target.click()
        # 선택한 명령 실행
        await cur_job()
        # 인덱스 증가 또는 초기화
        self.cur_idx += 1
        if self.cur_idx >= len(self.locator_idxs):
            self.cur_idx = 0
