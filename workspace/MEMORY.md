# Project Status: Schedule Management Assistant

## Database Location
- Path: `/app/workspace/data/schedule.db`
- Status: Initialized with `origin_urls`, `page_urls`, `todo_list` tables.

## Tracked Sources
- **Korea Univ Sejong Capstone Design Notices**: [https://software.korea.ac.kr/software/2758/subview.do](https://software.korea.ac.kr/software/2758/subview.do)
- Last Collection: 2026-05-03
- Status: 2026-1학기 schedules (application, plan, final presentation) have been extracted and stored in `todo_list`.

## Next Steps
- Periodically check for new notices in the tracked URLs.
- Remind the user of upcoming deadlines as the due dates approach.

## Future Tasks & Ideas
- **Authenticated Scraping (로그인 필요 사이트 크롤링)**:
  - **Goal**: 포털 등 로그인이 필수적인 폐쇄형 게시판에서 일정 수집.
  - **Strategy**: `requests.Session()` 또는 Playwright를 활용한 세션 유지.
  - **Security (Crucial)**: 
    - 절대로 채팅창이나 코드(`my_db.py`, `scraper.py` 등)에 ID/PW를 하드코딩하지 않는다.
    - `.env` 파일을 활용하여 환경 변수(`os.getenv`)로 자격 증명을 관리하거나, 브라우저 세션 쿠키를 우회 주입하는 방식을 사용한다.
- **Improved LMS Attachment Downloader**:
  - **Status**: Updated on 2026-05-17.
  - **Change**: Now crawls 'Modules' instead of just the 'Files' tab, which bypassing common authorization restrictions. Supports `.pdf`, `.docx`, `.pptx`, `.zip`, `.ipynb`, `.hwp`, `.pcapng`.
