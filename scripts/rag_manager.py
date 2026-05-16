# scripts/rag_manager.py
import os
import chromadb
from PyPDF2 import PdfReader
import docx

# 벡터 DB 저장 경로 (여기에 문서들의 의미가 수학적으로 저장됨)
DB_PATH = "/app/workspace/data/chroma_db"
DOCS_PATH = "/app/workspace/docs"

client = chromadb.PersistentClient(path=DB_PATH)
collection = client.get_or_create_collection(name="local_documents")

def ingest_documents():
    """docs 폴더의 PDF, DOCX 파일을 읽어서 DB에 학습시킵니다."""
    if not os.path.exists(DOCS_PATH):
        os.makedirs(DOCS_PATH)
        return "docs 폴더가 없어서 새로 만들었어. 문서를 먼저 넣어줘!"

    files = os.listdir(DOCS_PATH)
    if not files:
        return "docs 폴더에 읽을 문서가 하나도 없어!"

    count = 0
    for filename in files:
        filepath = os.path.join(DOCS_PATH, filename)
        text = ""
        
        try:
            # 1. PDF 읽기
            if filename.endswith(".pdf"):
                reader = PdfReader(filepath)
                for page in reader.pages:
                    extracted = page.extract_text()
                    if extracted: text += extracted + "\n"
            
            # 2. Word 읽기
            elif filename.endswith(".docx"):
                doc = docx.Document(filepath)
                for para in doc.paragraphs:
                    text += para.text + "\n"
            
            # 3. 텍스트를 청크(조각)로 쪼개서 DB에 넣기
            if text.strip():
                # 모델이 소화하기 좋게 500글자씩 쪼개기
                chunks = [text[i:i+500] for i in range(0, len(text), 500)]
                for i, chunk in enumerate(chunks):
                    doc_id = f"{filename}_chunk_{i}"
                    collection.add(
                        documents=[chunk],
                        metadatas=[{"filename": filename}],
                        ids=[doc_id]
                    )
                count += 1
        except Exception as e:
            print(f"[{filename}] 읽기 실패: {e}")

    return f"완료! 총 {count}개의 문서를 쪼개서 내 머릿속(DB)에 완벽하게 집어넣었어."

def search_local_docs(query: str) -> str:
    """사용자의 질문과 가장 관련된 문서 내용을 찾아옵니다."""
    # DB가 비어있는지 확인
    if collection.count() == 0:
        return "아직 학습된 문서가 없어. 먼저 문서 학습부터 시켜줘!"

    results = collection.query(
        query_texts=[query],
        n_results=3 # 가장 관련도 높은 3개의 조각을 찾아옴
    )
    
    if not results['documents'][0]:
        return "질문과 관련된 내용을 문서에서 찾을 수 없어."
        
    context = "관련 문서 내용:\n"
    for doc, meta in zip(results['documents'][0], results['metadatas'][0]):
        context += f"▶ [{meta['filename']}]\n{doc}\n\n"
    
    return context