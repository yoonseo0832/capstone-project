"""CLI: python -m lms <명령>

  init                     DB 2개 생성
  sync [--full]            Canvas 에서 지금 바로 수집
  cleanup                  완료된 일정 지금 바로 삭제
  scheduler [--run-now]    매일 아침 수집 + 자정 삭제 상주 실행
  list [--all] [--source lms]   일정 조회
  add "제목" [--due ...] [--content ...] [--url ...]   일정 직접 추가
  edit ID [--title ...] [--due ...] [--content ...]     일정 수정
  done ID [--undo]         완료 체크/해제
  delete ID                일정 삭제
  url add URL [--label ..] / url list / url remove URL   수집 URL 관리
  raw                      LMS 원본 DB 조회
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from common import schedule_db
from lms import raw_db
from lms.config import load_settings
from lms.sync import cleanup, init_all, sync_lms


def _print_rows(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        print("(없음)")
        return
    for r in rows:
        print(" | ".join(str(r.get(c) if r.get(c) is not None else "-") for c in cols))


def main(argv=None) -> int:
    # Windows 콘솔에서 한글 출력 깨짐 방지
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    p = argparse.ArgumentParser(prog="python -m lms", description="Canvas LMS 일정 수집기")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("sync").add_argument("--full", action="store_true", help="변경 없어도 일정 전부 다시 반영")
    sub.add_parser("cleanup")
    sub.add_parser("scheduler").add_argument("--run-now", action="store_true", help="시작하자마자 1회 수집")

    sp = sub.add_parser("list")
    sp.add_argument("--all", action="store_true", help="완료된 일정도 표시")
    sp.add_argument("--source")

    sp = sub.add_parser("add")
    sp.add_argument("title")
    sp.add_argument("--due", help="YYYY-MM-DD HH:MM")
    sp.add_argument("--start", help="YYYY-MM-DD HH:MM")
    sp.add_argument("--content")
    sp.add_argument("--url")

    sp = sub.add_parser("edit")
    sp.add_argument("id", type=int)
    sp.add_argument("--title")
    sp.add_argument("--due")
    sp.add_argument("--start")
    sp.add_argument("--content")
    sp.add_argument("--url")

    sp = sub.add_parser("done")
    sp.add_argument("id", type=int)
    sp.add_argument("--undo", action="store_true")

    sub.add_parser("delete").add_argument("id", type=int)

    sp = sub.add_parser("url")
    usub = sp.add_subparsers(dest="url_cmd", required=True)
    up = usub.add_parser("add")
    up.add_argument("url")
    up.add_argument("--label")
    usub.add_parser("list")
    usub.add_parser("remove").add_argument("url")

    sub.add_parser("raw")

    args = p.parse_args(argv)
    s = load_settings()
    init_all(s)
    db = s.schedule_db

    if args.cmd == "init":
        print(f"일정 DB: {s.schedule_db}\n원본 DB: {s.lms_raw_db}")
    elif args.cmd == "sync":
        try:
            print(sync_lms(s, full=args.full))
        except RuntimeError as e:
            print(f"에러: {e}", file=sys.stderr)
            return 1
    elif args.cmd == "cleanup":
        print(f"{len(cleanup(s))}건 삭제")
    elif args.cmd == "scheduler":
        from lms.scheduler import run_scheduler
        run_scheduler(s, run_now=args.run_now)
    elif args.cmd == "list":
        rows = schedule_db.list_schedules(db, include_completed=args.all, source=args.source)
        _print_rows(rows, ["id", "due_at", "title", "source", "is_completed"])
    elif args.cmd == "add":
        new_id = schedule_db.add_schedule(db, args.title, args.content, args.due, args.start, args.url)
        print(f"추가됨 id={new_id}")
    elif args.cmd == "edit":
        fields = {k: v for k, v in dict(title=args.title, due_at=args.due, start_at=args.start,
                                        content=args.content, url=args.url).items() if v is not None}
        print(json.dumps(schedule_db.update_schedule(db, args.id, **fields), ensure_ascii=False, indent=2))
    elif args.cmd == "done":
        row = schedule_db.set_completed(db, args.id, not args.undo)
        print("완료 처리" if not args.undo else "완료 해제", "→", row and row["title"])
    elif args.cmd == "delete":
        print("삭제됨" if schedule_db.delete_schedule(db, args.id) else "해당 id 없음")
    elif args.cmd == "url":
        if args.url_cmd == "add":
            print("추가됨" if raw_db.add_url(s.lms_raw_db, args.url, "user", args.label) else "이미 있음")
        elif args.url_cmd == "list":
            _print_rows(raw_db.list_urls(s.lms_raw_db), ["id", "kind", "url", "label", "last_fetched_at"])
        else:
            print("삭제됨" if raw_db.remove_url(s.lms_raw_db, args.url) else "해당 URL 없음")
    elif args.cmd == "raw":
        _print_rows(raw_db.list_items(s.lms_raw_db),
                    ["source_id", "due_at", "course_name", "title", "submitted", "is_done", "content_hash"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
