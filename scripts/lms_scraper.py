# scripts/lms_scraper.py
import os
import sys
from datetime import datetime
from canvasapi import Canvas
from dotenv import load_dotenv

# 1. 환경 변수 강제 로드 (API 키 가져오기)
load_dotenv(dotenv_path="/app/.env")

# 2. 팀원이 만든 DB 도구(my_db.py) 불러오기
sys.path.append("/app/scripts")
try:
    from my_db import insert_db_page_urls, insert_db_todo_list
except ImportError:
    print("에러: my_db.py를 불러올 수 없습니다.")
    sys.exit(1)

def fetch_lms_and_save_to_db():
    api_url = "https://mylms.korea.ac.kr"
    api_key = os.environ.get("CANVAS_API_KEY")

    if not api_key:
        print("에러: .env 파일에서 CANVAS_API_KEY를 찾을 수 없습니다.")
        return

    print("LMS(Canvas) 서버에서 과제 데이터를 요청하는 중...")
    try:
        canvas = Canvas(api_url, api_key)
        courses = canvas.get_courses(enrollment_state="active")
        
        db_path = "/app/workspace/data/schedule.db"
        count = 0

        for course in courses:
            if not hasattr(course, 'name'): 
                continue
            
            assignments = course.get_assignments()
            for assignment in assignments:
                # 마감일이 있는 과제만 DB에 저장
                if hasattr(assignment, 'due_at') and assignment.due_at:
                    dt = datetime.strptime(assignment.due_at, "%Y-%m-%dT%H:%M:%SZ")
                    due_date = dt.strftime("%Y-%m-%d %H:%M")
                    content = f"[LMS 과제] {course.name} - {assignment.name}"
                    url = assignment.html_url

                    # 팀원 스크래퍼와 동일하게 DB에 차곡차곡 저장!
                    insert_db_page_urls(db_path, url)
                    insert_db_todo_list(db_path, url, content, due_date)
                    count += 1
        
        print(f"성공! 총 {count}개의 기한 있는 과제를 DB에 업데이트했습니다.")

    except Exception as e:
        print(f"LMS 연동 중 에러 발생: {e}")

if __name__ == "__main__":
    fetch_lms_and_save_to_db()