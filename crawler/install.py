"""크롤러가 쓰는 Playwright 브라우저(Chromium) 설치.

pip install -r requirements.txt 는 Playwright '라이브러리'만 설치하고, 실제로 띄울 브라우저는
따로 내려받아야 한다. 처음 한 번만 실행하면 된다:  python -m crawler install
"""
from __future__ import annotations

import subprocess
import sys

BROWSER = "chromium"


def install_browser() -> int:
    """지금 쓰는 파이썬(.venv)의 Playwright 로 Chromium 을 설치한다. 반환값: 종료 코드(0 = 성공)."""
    cmd = [sys.executable, "-m", "playwright", "install", BROWSER]
    print(f"🔧 크롤러용 브라우저({BROWSER})를 설치합니다. 처음 한 번만 하면 되고 몇 분 걸릴 수 있어요.")
    print("   $ " + " ".join(cmd))
    try:
        code = subprocess.call(cmd)
    except OSError as e:
        print(f"⚠️ 설치 명령을 실행하지 못했어요: {e}")
        return 1

    if code != 0:
        print("⚠️ 설치에 실패했어요. 확인할 것:")
        print("   1) .venv 가 활성화되어 있는지 (pip install -r requirements.txt 를 먼저 했는지)")
        print("   2) 인터넷에 연결되어 있는지 (브라우저를 내려받음)")
        return code

    print("✅ 설치 완료! 이제 python -m crawler run 으로 크롤링할 수 있어요.")
    if sys.platform.startswith("linux"):
        # Linux(WSL 포함)는 브라우저가 쓰는 시스템 라이브러리가 없으면 실행할 때 오류가 난다
        print("ℹ️ Linux/WSL 에서 크롤링할 때 'missing dependencies' 오류가 나면 아래를 한 번 실행하세요:")
        print(f"   sudo {sys.executable} -m playwright install-deps {BROWSER}")
    return 0


def is_browser_missing_error(message: str) -> bool:
    """Playwright 가 '브라우저가 설치되지 않았다'고 낸 오류인지 (예: Executable doesn't exist ...)."""
    text = message.lower()
    return "executable doesn't exist" in text or "playwright install" in text


if __name__ == "__main__":
    sys.exit(install_browser())
