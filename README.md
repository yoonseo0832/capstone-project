# Canvas LMS 일정 수집기

Canvas LMS API로 과제를 매일 아침 받아와서 **팀 공유 일정 DB**에 넣는다.
원본 데이터는 개인 DB(`lms_raw.db`)에 따로 저장한다.

## 구조

```
common/schedule_db.py   팀 공유: schedules 테이블 CRUD (다른 크롤러/답변 모듈도 이것만 import)
lms/canvas_client.py    Canvas API 호출 + 정규화 (UTC → KST, HTML → 텍스트/링크 추출)
lms/raw_db.py           LMS 원본 DB: source_urls, raw_items(내용/링크/해시)
lms/sync.py             원본 저장 → 해시 비교 → 바뀐 것만 일정 반영 / 완료 일정 정리
lms/scheduler.py        매일 07:00 수집, 00:00 완료 일정 삭제
lms/__main__.py         CLI
tests/                  pytest (가짜 Canvas 클라이언트, API 키 없이 실행됨)
```

## DB

### 1) `schedule.db` — 공유 (`schedules`)
| 컬럼 | 설명 |
|---|---|
| id | PK |
| title | 제목 |
| content | 내용 |
| start_at / due_at | 일정 시작 / 날짜·마감 (`YYYY-MM-DD HH:MM`, KST) |
| url | 원문 링크 |
| source | `lms`, `crawler:<사이트>`, `user` |
| source_id | 각자 원본 DB의 고유키. `(source, source_id)` UNIQUE |
| is_completed / completed_at | 완료 여부 |
| created_at / updated_at | |

다른 크롤러는 `schedule_db.upsert_by_source(db, "crawler:xxx", 고유키, title=..., due_at=...)` 로 넣으면
중복 없이 추가·갱신된다.

### 2) `lms_raw.db` — LMS 개인용
- `source_urls`: 수집한 강의 URL(`lms_course`) + 사용자가 추가한 URL(`user`)
- `raw_items`: 원본 내용(텍스트/HTML), 원문 링크, 추출 링크(JSON), `content_hash`(SHA-256), 제출 여부, `is_done`

## 동작 규칙
- 해시가 같으면(내용 변경 없으면) 일정 테이블을 건드리지 않음 → 사용자가 지운 일정이 다시 생기지 않음
- 마감 지난 과제·마감 없는 과제는 일정에 올리지 않음 (원본에는 저장). `.env`로 변경 가능
- Canvas에서 **제출됨** → 일정 자동 완료 처리
- 매일 **00:00** 완료된 일정 삭제 → LMS 항목은 원본 DB에 `is_done=1` 표시해서 다음 수집 때 재등록 안 함
- 사용자가 추가한 URL이 이 LMS의 `/courses/<id>` 이면 해당 강의도 수집 대상에 포함

## 사용법

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # CANVAS_API_KEY 입력

python -m lms init              # DB 2개 생성
python -m lms sync              # 지금 바로 수집 (--full: 전부 다시 반영)
python -m lms list              # 일정 조회 (--all: 완료 포함)
python -m lms add "중간고사" --due "2026-10-20 10:00"
python -m lms edit 3 --due "2026-10-21 10:00"
python -m lms done 3            # 완료 체크 (--undo 해제)
python -m lms delete 3
python -m lms cleanup           # 완료 일정 지금 삭제
python -m lms url add https://mylms.korea.ac.kr/courses/12345 --label "추가 강의"
python -m lms url list
python -m lms raw               # 원본 DB 확인
python -m lms scheduler --run-now   # 상주 실행: 매일 07:00 수집 / 00:00 정리

pytest -q
```
