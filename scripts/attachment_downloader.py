# scripts/attachment_downloader.py
import os
import requests
from canvasapi import Canvas
from dotenv import load_dotenv

# 환경변수 로드
load_dotenv(dotenv_path="/app/.env")

DOCS_DIR = "/app/workspace/docs"

def download_lms_files():
    """LMS의 활성화된 강의에서 파일들을 찾아 docs 폴더로 다운로드합니다."""
    api_url = "https://mylms.korea.ac.kr"
    api_key = os.environ.get("CANVAS_API_KEY")

    if not api_key:
        return "에러: CANVAS_API_KEY가 없습니다."

    # docs 폴더가 없으면 생성
    if not os.path.exists(DOCS_DIR):
        os.makedirs(DOCS_DIR)

    try:
        canvas = Canvas(api_url, api_key)
        courses = canvas.get_courses(enrollment_state="active")
        
        download_count = 0
        downloaded_files = []

        for course in courses:
            if not hasattr(course, 'name'): continue
            
            # 강의의 파일 목록 가져오기
            try:
                files = course.get_files()
                for file in files:
                    # PDF나 Word 파일만 타겟팅
                    if file.filename.endswith(('.pdf', '.docx')):
                        file_path = os.path.join(DOCS_DIR, file.display_name)
                        
                        # 이미 다운받은 파일이면 패스 (중복 방지)
                        if os.path.exists(file_path):
                            continue
                        
                        # 파일 다운로드 실행
                        response = requests.get(file.url)
                        if response.status_code == 200:
                            with open(file_path, 'wb') as f:
                                f.write(response.content)
                            downloaded_files.append(file.display_name)
                            download_count += 1
            except Exception as e:
                # 권한이 없어서 파일을 못 읽는 강의는 가볍게 무시
                pass
                
        if download_count == 0:
            return "새로 다운로드할 첨부파일이 없습니다. (모두 최신 상태입니다)"
            
        return f"총 {download_count}개의 새로운 파일을 다운로드했습니다!\n" + "\n".join([f"- {f}" for f in downloaded_files])

    except Exception as e:
        return f"파일 다운로드 중 에러 발생: {str(e)}"

if __name__ == "__main__":
    print(download_lms_files())