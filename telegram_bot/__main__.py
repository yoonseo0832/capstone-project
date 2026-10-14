"""python -m telegram_bot  — 텔레그램 봇 실행 (Ctrl+C 로 종료)"""
import sys

from telegram_bot.app import run

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        run()
    except RuntimeError as e:
        print(f"에러: {e}", file=sys.stderr)
        sys.exit(1)
