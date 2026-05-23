from src.config.settings import build_llm
from src.rag.vector_store import get_vector_store

ANSWER_PROMPT = """You are an expert SRE/DevOps AI assistant. Answer based on the knowledge base context.
Be specific and actionable. Cite which source document your answer comes from.

Context:
{context}

Question: {question}

Answer:"""

class RAGRetriever:
    def __init__(self):
        self.store = get_vector_store()
        self.llm = build_llm()

    async def query(self, question: str, top_k: int = 5) -> dict:
        hits = await self.store.search(question, top_k=top_k)
        if not hits:
            return {"answer": "No relevant documents found.", "sources": [], "confidence": 0.0}

        context = "\n---\n".join(f"[{h['metadata'].get('source','?')}]\n{h['content']}" for h in hits)
        prompt = ANSWER_PROMPT.format(context=context, question=question)
        response = await self.llm.ainvoke(prompt)
        answer = response.content if hasattr(response, "content") else str(response)

        return {
            "answer": answer,
            "sources": [{"source": h["metadata"].get("source",""), "relevance": round(h["score"],3)} for h in hits],
            "confidence": round(sum(h["score"] for h in hits) / len(hits), 3),
        }
