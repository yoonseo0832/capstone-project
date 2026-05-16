# syntax=docker/dockerfile:1
FROM node:20-slim

# 필요한 빌드 도구 설치 (C++ 및 Python 환경)
RUN apt-get update && apt-get install -y \
    python3 python3-pip \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# 2. tzdata 패키지 설치 및 타임존 설정 (Debian/Ubuntu 계열)
ENV TZ=Asia/Seoul
RUN apt-get install -y tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone

# Gemini CLI 설치
RUN npm install -g @google/gemini-cli

# 파이썬 라이브러리 설치
RUN pip3 install --no-cache-dir --break-system-packages mcp fastmcp beautifulsoup4 requests canvasapi python-dotenv PyPDF2 python-docx chromadb

# 작업 디렉토리 설정
WORKDIR /app
COPY . .

# 권한 설정 및 실행
CMD ["bash"]