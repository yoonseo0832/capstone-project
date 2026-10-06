# 일정 비서 — LMS 수집 + 공유 일정 DB

> 담당: Yoonseo (Canvas LMS 수집, 공유 일정 DB, 시연용 답변 비서)

Canvas LMS API로 과제를 **매일 아침 자동으로** 받아와서 **팀 공유 일정 DB**에 넣고,
Gemini API로 "이번 주 마감 뭐 있어?" 같은 질문에 **1~2초 안에** 답한다.

---

## 1. 담당 범위

| 구분 | 내용 |
|---|---|
| ✅ 내 담당 | Canvas LMS API 수집 → LMS 원본 DB → **공유 일정 DB**, 일정 DB 생성·추가·수정·삭제, 매일 07:00 수집 / 00:00 완료 일정 삭제, 시연용 답변 비서 |
| 🤝 팀 공유 | **`schedules` 테이블 하나만** 공유한다. 모든 모듈은 [common/schedule_db.py](common/schedule_db.py) 함수로만 접근한다 |

## 2. 전체 흐름

```
 [Canvas LMS API] ──(매일 07:00)──▶ lms/sync.py
                                     │ ① 원본 저장 + 해시 비교   → lms_raw.db (내 원본 DB)
                                     │ ② 새 과제/바뀐 과제만 반영 ─┐
 [다른 사이트 크롤러] (팀원) ───────────────────────────────────┤
 [사용자 직접 추가] ───────────────────────────────────────────┤
                                                                 ▼
                                               schedule.db / schedules  ◀── 팀 공유
                                                                 │
                         (매일 00:00) 완료된 일정 삭제 ◀──────────┤
                                                                 ▼
                                    assistant (Gemini API) ── "이번 주 마감?" → 1~2초 답변
```

## 3. 빠른 시작

```bash
python -m venv .venv
.venv\Scripts\activate                 # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                   # CANVAS_API_KEY, GEMINI_API_KEY 입력

python -m lms sync                     # LMS 과제 수집 (지금 바로)
python -m lms list                     # 공유 일정 확인
python -m assistant                    # 비서와 대화
```

- **CANVAS_API_KEY**: LMS → 계정 → 설정 → "새 액세스 토큰"
- **GEMINI_API_KEY**: https://aistudio.google.com/apikey (무료)
- `.env`, `*.db` 는 `.gitignore` 에 있어서 **GitHub에 올라가지 않는다** (키·개인 과제 데이터 보호)

### 비서 사용법
```bash
python -m assistant                              # 대화 모드 (앞 대화 기억, 종료: exit)
python -m assistant "이번 주 마감 뭐 있어?"         # 한 번만 질문 (따옴표 필수)
python -m assistant --timing "다음 주는?"         # 응답 시간 + 실제 답한 모델 표시
python -m assistant --model gemini-2.5-flash "…" # 모델 지정
```

| 하고 싶은 것 | 이렇게 말하기 |
|---|---|
| 조회 | "이번 주 마감 뭐 있어?", "다음 주는?", "빅데이터개론 남은 과제", "10월 안에 끝내야 하는 거" |
| 완료 | "Week 04 Lab 끝냈어", "Assignment1 제출했어" |
| 추가 | "10월 20일 오후 3시에 캡스톤 회의 추가해줘" |
| 수정 | "캡스톤 회의 10월 21일로 바꿔줘" |
| 삭제 | "캡스톤 회의 삭제해줘" ("삭제/지워" 라고 해야 지움) |
| LMS 새로고침 | "LMS 새로고침해줘" |

대상이 여러 개면(예: "과제 끝냈어") 추측하지 않고 되묻는다. **개수 질문("몇 개야?")은 틀릴 수 있으니 목록으로 물어보기** (7장).

AI 없이 확인·수정: `python -m lms list` / `done <id>` / `add "제목" --due "YYYY-MM-DD HH:MM"` / `delete <id>`

| 메시지 | 해결 |
|---|---|
| `모든 모델이 바쁘거나 무료 사용량을 초과` | 1분 뒤 다시 질문 |
| `GEMINI_API_KEY 가 없습니다` | `.env` 에 키 입력 |
| `ModuleNotFoundError` | `.venv\Scripts\activate` 먼저 |
| 일정이 비어 있음 | `python -m lms sync` 먼저 |
| 입력이 안 됨 (VS Code 밖 Git Bash 창) | PowerShell 사용 또는 `winpty python -m assistant` |

---

## 4. 팀원이 알아야 할 것: 공유 일정 테이블

### `schedule.db` → `schedules`
| 컬럼 | 설명 |
|---|---|
| `id` | PK |
| `title` | 제목 |
| `content` | 내용 (LMS 과제는 `[과목명] 설명` 형태) |
| `start_at` / `due_at` | 시작 / 날짜·마감. **`YYYY-MM-DD HH:MM`, 한국 시간** |
| `url` | 원문 링크 |
| `source` | 출처: `lms`, `user`, `crawler:<사이트이름>` |
| `source_id` | 각자 원본 DB의 고유키. **`(source, source_id)` 는 UNIQUE** |
| `is_completed` / `completed_at` | 완료 여부 / 완료 시각 |
| `created_at` / `updated_at` | 생성 / 수정 시각 |

### 크롤러 담당: 일정 넣기
```python
from common import schedule_db

DB = "workspace/data/schedule.db"
schedule_db.init_db(DB)   # 테이블 없으면 생성 (여러 번 호출해도 안전)

# 같은 (source, source_id) 가 있으면 갱신, 없으면 추가 → 매일 돌려도 중복 안 생김
schedule_db.upsert_by_source(
    DB, source="crawler:software_notice", source_id="article:267087",
    title="캡스톤 결과보고서 제출", due_at="2026-11-20 17:00",
    content="학과 메일로 제출", url="https://software.korea.ac.kr/...",
)
```

### 답변 담당: 일정 읽기·수정
```python
schedule_db.list_schedules(DB, include_completed=False)              # 미완료 전체
schedule_db.list_schedules(DB, due_after="2026-10-06 00:00", due_before="2026-10-13 00:00")
schedule_db.set_completed(DB, schedule_id)                           # 완료 처리
schedule_db.update_schedule(DB, schedule_id, due_at="2026-10-21 10:00")
schedule_db.delete_schedule(DB, schedule_id)
```

### 규칙
1. **SQL을 직접 쓰지 말고 `schedule_db` 함수만** 쓴다. 테이블 구조가 바뀌어도 함수만 고치면 된다.
2. 시간은 **로컬(한국) 시간 `YYYY-MM-DD HH:MM`** 문자열. UTC 그대로 넣으면 9시간 어긋난다.
3. 원본 데이터(본문, 해시 등)는 **각자 자기 DB**에 둔다. 공유 테이블에는 일정에 필요한 것만 넣는다.
4. 완료된 일정은 **매일 00:00에 삭제**된다. 삭제된 행은 `cleanup_completed()` 가 반환하므로, 각 크롤러가 원본 DB에 "완료됨"을 표시하면 다음 수집 때 다시 생기지 않는다 (LMS 쪽 구현 참고: [lms/sync.py](lms/sync.py) `cleanup()`).

---

## 5. 파일별 역할과 설계 이유

### `common/` — 팀 공유
| 파일 | 하는 일 | 왜 이렇게 만들었나 |
|---|---|---|
| [schedule_db.py](common/schedule_db.py) | `schedules` 테이블 생성·추가·조회·수정·삭제, `upsert_by_source`, `cleanup_completed` | **공유 테이블의 유일한 입구**. 모든 SQL에 `?` 파라미터를 써서 SQL 인젝션을 막았다 (이전 코드는 문자열을 이어 붙였다). 수정 가능한 컬럼은 화이트리스트로 제한. `(source, source_id)` UNIQUE 로 여러 크롤러가 같은 테이블에 넣어도 서로 덮어쓰지 않는다. `upsert` 는 `is_completed=None` 이면 완료 여부를 건드리지 않아서, 사용자가 직접 체크한 완료 상태가 다음 수집 때 풀리지 않는다 |

### `lms/` — LMS 수집 (내 담당)
| 파일 | 하는 일 | 왜 이렇게 만들었나 |
|---|---|---|
| [config.py](lms/config.py) | `.env` 에서 키·DB 경로·스케줄 시간을 읽음 | 키를 코드에 쓰지 않기 위해. 경로·시간을 코드 수정 없이 바꿀 수 있고, 테스트에서는 임시 경로를 넘긴다 |
| [canvas_client.py](lms/canvas_client.py) | Canvas API로 강의·과제·제출 상태를 받아 dict 로 정규화 | Canvas 는 시간을 **UTC** 로 줘서 한국 시간으로 변환한다 (이전 코드는 변환이 없어서 9시간 차이 났음). 본문 HTML에서 텍스트와 링크를 분리 저장. 다른 코드는 Canvas 라이브러리를 몰라도 되도록 여기서만 Canvas 를 다룬다 → 테스트에서 가짜 클라이언트로 바꿔 끼울 수 있다 |
| [raw_db.py](lms/raw_db.py) | LMS 원본 DB: `raw_items`(본문, HTML, 링크, 추출 링크, **해시**, 제출 여부), `source_urls`(강의 URL + 사용자 URL) | 요구사항 "원본 데이터 저장 [내용, 링크, 해시]". 해시(SHA-256)는 제목·본문·마감·링크로 만들어서 **내용이 바뀐 과제만** 공유 테이블에 반영한다. 제출 여부는 해시에서 빼고 따로 비교 (제출은 "내용 변경"이 아니라 "완료"이므로) |
| [sync.py](lms/sync.py) | 수집 → 원본 저장 → 해시 비교 → 필요한 것만 일정 반영 / 자정 정리 | 일정 테이블은 **신규·내용 변경·제출 상태 변경**일 때만 건드린다 → 사용자가 지운 일정이 매일 아침 되살아나지 않는다. 마감 지난 과제·마감 없는 과제는 일정에서 빼고 원본에만 저장(`.env` 로 변경 가능). 강의 하나가 에러 나도 나머지는 계속 수집 |
| [scheduler.py](lms/scheduler.py) | 매일 07:00 수집, 00:00 완료 일정 삭제 | 요구사항 "아침에 한 번씩", "12시쯤 완료 과제 삭제". APScheduler 로 파이썬 하나만 띄우면 돼서 Windows/Linux 동일하게 동작. PC가 꺼져 있다 켜져도 1시간 안이면 밀린 작업 실행 |
| [\_\_main\_\_.py](lms/__main__.py) | CLI: `sync`, `list`, `add`, `edit`, `done`, `delete`, `cleanup`, `url`, `raw`, `scheduler` | DB 를 직접 열지 않고 확인·수정·시연할 수 있게 |

### `assistant/` — 시연용 답변 비서
| 파일 | 하는 일 | 왜 이렇게 만들었나 |
|---|---|---|
| [context.py](assistant/context.py) | DB 일정을 프롬프트용 짧은 표로 변환. 과목명 축약, **요일·D-day·이번주/다음주를 코드가 미리 계산** | 일정이 수십 건이라 매번 통째로 넣는 게 가장 빠르다 (조회용 왕복 없음). 가벼운 모델이 요일·날짜 계산을 자주 틀려서(10/07을 목요일로, 11/18을 10/18로) 계산은 코드가, 모델은 복사만 하게 했다 |
| [bot.py](assistant/bot.py) | Gemini API 호출 (스트리밍), 완료/추가/수정/삭제/LMS 새로고침 함수 호출 처리, 대화 기록 | **질문 1개 = LLM 호출 1번**. 함수 호출 뒤 결과 안내 문장은 코드가 만들어서 두 번째 LLM 호출이 없다. 무료 한도 초과(429) 시 친절한 안내 |
| [\_\_main\_\_.py](assistant/__main__.py) | 대화 모드 / 한 번 질문 / `--timing` 응답 시간 표시 | 시연 및 속도 측정용 |

### 기타
| 파일 | 하는 일 |
|---|---|
| [tests/](tests/) | pytest 20개. 가짜 Canvas·가짜 Gemini 로 **API 키 없이** 돌아간다 (중복 방지, 해시 변경 감지, 완료→삭제→재등록 방지, 함수 호출 등) |
| [.env.example](.env.example) | `.env` 템플릿 (실제 키는 넣지 않음) |
| [requirements.txt](requirements.txt) | 의존성 |

---

## 6. 동작 규칙 요약
- 해시가 같으면(내용 그대로면) 일정 테이블을 건드리지 않는다
- Canvas 에서 **제출됨** → 일정 자동 완료 처리
- 매일 **00:00** 완료 일정 삭제 → LMS 항목은 원본 DB에 `is_done=1` → 다음 수집 때 재등록 안 됨
- 마감 지난 과제·마감 없는 과제는 일정에 안 올림 (원본엔 저장) — `.env` 의 `LMS_SKIP_PAST_DUE`, `LMS_INCLUDE_NO_DUE`
- 사용자가 추가한 URL이 이 LMS의 `/courses/<id>` 면 그 강의도 수집 (`python -m lms url add ...`)

## 7. LLM 선택 기록 (왜 Gemini API 직접 호출인가)

| 시도 | 결과 |
|---|---|
| Gemini CLI | 2026-06-18 무료/Pro 요금제 지원 종료 → 사용 불가 |
| Antigravity CLI (`agy`) + MCP | 동작은 했지만 **코딩 에이전트**라 질문마다 긴 시스템 프롬프트 + 도구 호출 왕복으로 수십 초 걸림. 웹/앱에 붙일 수도 없음 → **제거** |
| **Gemini API 직접 호출** (현재) | 첫 글자 **약 1.1~1.8초**, 전체 2초 안팎 (실측) |

기본 모델 `gemini-2.5-flash-lite` (thinking 끔). `.env` 의 `GEMINI_MODEL` 로 변경 가능.

**자동 전환(fallback)**: 기본 모델이 과부하(503)·무료 한도 초과(429)면
`gemini-2.5-flash` → `gemini-3.5-flash-lite` 순서로 자동 재시도한다 (무료 한도는 모델별이라 바꾸면 대개 통과).
답하던 도중 끊기면 "응답이 끊겨서 다시 답할게요" 안내 후 다음 모델로 처음부터 답한다.
순서는 `.env` 의 `GEMINI_FALLBACK_MODELS=a,b` 로 변경.
실제로 기본 모델이 응답하지 못했을 때 `gemini-2.5-flash` 로 넘어가 2초 안에 답한 것을 확인함.

**무료 vs 유료**: 평소 개발은 무료 티어 + 자동 전환으로 충분하다. 시연 당일 안정성이 필요하면
AI Studio 에서 키의 프로젝트에 Google Cloud 결제를 연결해 유료 티어로 전환 (한도 대폭 증가, 코드 수정 없음).
- Gemini 앱 구독(Google AI Pro)은 API 사용량과 별개로 알고 있음 → API 는 결제 연결이 따로 필요 (확인 필요)
- Cloud 예산 **알림**을 걸어둘 것 (자동 차단 상한이 아니라 알림)
- 유료여도 서버 과부하(503)는 생길 수 있어 자동 전환은 계속 필요

**알려진 한계 (실측)** — 정답이 정해진 질문 6개로 비교:
- "이번 주 / 다음 주 / 특정 기간" 일정 목록은 정확
- **"몇 개야?" 같은 개수 세기는 틀리는 경우가 있음** (11월 마감 6개 → 2개로 답함)
- 같은 질문에 `gemini-3.5-flash-lite` 는 비슷한 속도(첫 글자 ~1.5초)였고 개수 질문 하나는 맞혔지만 다른 하나는 틀림
- 개선 아이디어: 과목별·월별 개수를 코드가 미리 계산해서 프롬프트에 넣기 (날짜 계산 문제를 이 방식으로 해결함)

## 8. 테스트
```bash
pytest -q
```
실제 LMS 로 처음 확인할 때는 테스트용 DB 로:
```bash
# PowerShell: $env:SCHEDULE_DB_PATH="workspace/data/test_schedule.db"; $env:LMS_RAW_DB_PATH="workspace/data/test_lms_raw.db"
export SCHEDULE_DB_PATH=workspace/data/test_schedule.db LMS_RAW_DB_PATH=workspace/data/test_lms_raw.db
python -m lms sync && python -m lms raw
```

## 9. 남은 일 / 정해야 할 것
- [ ] 다른 크롤러와 합치기: `upsert_by_source` 로 `schedules` 에 넣기만 하면 됨 (4장)
- [ ] 개수 질문 정확도 개선 (7장)
- [ ] `Dockerfile` / `docker-compose.yml` 은 **이전 구조(Gemini CLI) 용**이라 현재 동작하지 않음 — 쓸지 정하고 고치거나 삭제
- [ ] 정식 서비스로 간다면: 사용자별 LMS 토큰 입력(또는 학교에 OAuth Developer Key 요청), 토큰 암호화 저장, `schedules` 에 `user_id` 추가 — **캡스톤 시연 범위에서는 하지 않음**
