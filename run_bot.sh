#!/usr/bin/env bash
set -e                                   # 중간에 오류가 나면 바로 멈춤
cd "$(dirname "$0")"                     # 어디서 실행하든 이 파일이 있는 폴더(프로젝트 루트)로 이동
exec .venv/bin/python -m telegram_bot "$@"