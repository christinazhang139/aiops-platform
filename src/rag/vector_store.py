from functools import lru_cache
import chromadb
from src.rag.loader import Document

COLLECTION_NAME = "ops_knowledge"

class VectorStore:
    def __init__(self):
        self.client = chromadb.Client()
        self.collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )

    async def add_documents(self, documents: list[Document]):
        if not documents:
            return
        ids = [doc.metadata.get("chunk_id", str(i)) for i, doc in enumerate(documents)]
        self.collection.upsert(
            ids=ids,
            documents=[d.content for d in documents],
            metadatas=[d.metadata for d in documents],
        )

    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        results = self.collection.query(query_texts=[query], n_results=top_k, include=["documents", "metadatas", "distances"])
        hits = []
        if results["documents"] and results["documents"][0]:
            for doc, meta, dist in zip(results["documents"][0], results["metadatas"][0], results["distances"][0]):
                hits.append({"content": doc, "metadata": meta, "score": 1 - dist})
        return hits

@lru_cache
def get_vector_store() -> VectorStore:
    return VectorStore()
