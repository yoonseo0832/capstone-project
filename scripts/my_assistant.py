# scripts/my_assistant.py
from fastmcp import FastMCP
import os
import platform
import subprocess
from rag_manager import ingest_documents, search_local_docs
from attachment_downloader import download_lms_files

# 1. 서버 초기화
mcp = FastMCP("MyPersonalAssistant")

@mcp.tool()
def get_system_status() -> str:
    """현재 비서가 작동 중인 시스템의 정보를 반환합니다."""
    return f"운영체제: {platform.system()}, 경로: {os.getcwd()}"

# --- [버튼 1] 팀원이 만든 학과 스크래퍼 실행 ---
@mcp.tool()
def run_teammate_scraper() -> str:
    """팀원이 작성한 scraper.py를 실행하여 최신 학사 공지를 DB에 업데이트합니다."""
    try:
        result = subprocess.run(["python", "/app/scripts/scraper.py"], capture_output=True, text=True, check=True)
        return f"학과 스크래퍼 실행 완료:\n{result.stdout.strip()}"
    except subprocess.CalledProcessError as e:
        return f"학과 스크래퍼 에러 발생:\n{e.stderr}"

# --- [버튼 2] 방금 만든 LMS 스크래퍼 실행 ---
@mcp.tool()
def run_lms_scraper() -> str:
    """lms_scraper.py를 실행하여 최신 LMS 과제를 DB에 업데이트합니다."""
    try:
        result = subprocess.run(["python", "/app/scripts/lms_scraper.py"], capture_output=True, text=True, check=True)
        return f"LMS 스크래퍼 실행 완료:\n{result.stdout.strip()}"
    except subprocess.CalledProcessError as e:
        return f"LMS 스크래퍼 에러 발생:\n{e.stderr}"

if __name__ == "__main__":
    mcp.run()

# --- [추가된 도구 1] 내 로컬 파일 학습하기 ---
@mcp.tool()
def learn_my_documents() -> str:
    """workspace/docs 폴더에 있는 사용자의 PDF, Word 파일들을 읽고 데이터베이스에 학습시킵니다."""
    return ingest_documents()

# --- [추가된 도구 2] 로컬 문서 기반으로 질문 답변하기 ---
@mcp.tool()
def answer_from_documents(question: str) -> str:
    """사용자가 로컬 문서 내용에 대해 질문할 때, 이 도구로 관련 내용을 검색한 뒤 답변해야 합니다."""
    # 문서 내용을 DB에서 찾아옴
    context = search_local_docs(question)
    
    # 찾은 내용을 바탕으로 나(제미나이)한테 힌트를 주는 용도
    return f"아래 문서 내용을 바탕으로 사용자의 질문 '{question}'에 답변해 줘:\n{context}"

# --- [도구 추가] 첨부파일 전용 다운로더 ---
@mcp.tool()
def fetch_and_download_attachments() -> str:
    """LMS 서버에 접속하여 새로운 강의 자료(PDF, Word)를 로컬 docs 폴더로 다운로드합니다."""
    return download_lms_files()