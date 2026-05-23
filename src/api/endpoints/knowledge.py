from fastapi import APIRouter
from pydantic import BaseModel
from src.rag.retriever import RAGRetriever
from src.rag.indexer import KnowledgeIndexer

router = APIRouter()

class QueryRequest(BaseModel):
    question: str
    top_k: int = 5

@router.post("/query")
async def query_knowledge(req: QueryRequest):
    retriever = RAGRetriever()
    return await retriever.query(req.question, top_k=req.top_k)

@router.post("/index")
async def index_documents():
    indexer = KnowledgeIndexer()
    count = await indexer.index_all()
    return {"message": "Indexing complete", "documents_indexed": count}
