"""일정 비서 실행.

  python -m assistant                  대화 모드 (종료: exit / quit / 빈 줄 두 번)
  python -m assistant "이번 주 마감?"    한 번만 질문
  --timing                             첫 글자/전체 응답 시간 표시
"""
from __future__ import annotations

import argparse
import sys
import time

from assistant.bot import ScheduleAssistant
from lms.config import load_settings
from lms.sync import init_all


def answer(bot: ScheduleAssistant, question: str, timing: bool) -> None:
    start = time.perf_counter()
    first = None
    for piece in bot.ask(question):
        if first is None:
            first = time.perf_counter() - start
        print(piece, end="", flush=True)
    print()
    if timing:
        print(f"  (첫 글자 {first or 0:.2f}s / 전체 {time.perf_counter() - start:.2f}s, 모델 {bot.last_model})")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="python -m assistant", description="일정 비서 (Gemini API)")
    p.add_argument("question", nargs="*", help="한 번만 물어볼 질문 (없으면 대화 모드)")
    p.add_argument("--timing", action="store_true", help="응답 시간 표시")
    p.add_argument("--model", help="Gemini 모델 (기본: .env GEMINI_MODEL 또는 gemini-2.5-flash-lite)")
    args = p.parse_args(argv)

    settings = load_settings()
    init_all(settings)
    try:
        bot = ScheduleAssistant(settings, model=args.model)
    except RuntimeError as e:
        print(f"에러: {e}", file=sys.stderr)
        return 1

    if args.question:
        answer(bot, " ".join(args.question), args.timing)
        return 0

    print(f"📅 일정 비서 ({bot.model}) — 종료하려면 exit")
    while True:
        try:
            q = input("\n나> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if q.lower() in ("exit", "quit", "종료"):
            return 0
        if q:
            print("비서> ", end="")
            answer(bot, q, args.timing)


if __name__ == "__main__":
    sys.exit(main())
