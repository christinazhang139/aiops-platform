import hashlib
from langchain_text_splitters import RecursiveCharacterTextSplitter
from src.config.settings import get_settings
from src.rag.loader import DocumentLoader, Document
from src.rag.vector_store import get_vector_store

class KnowledgeIndexer:
    def __init__(self):
        settings = get_settings()
        self.loader = DocumentLoader(settings.knowledge_base_dir)
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap,
            separators=["\n## ", "\n### ", "\n\n", "\n", " "],
        )
        self.store = get_vector_store()

    async def index_all(self) -> int:
        documents = self.loader.load_all()
        if not documents:
            return 0
        chunks = self._chunk(documents)
        await self.store.add_documents(chunks)
        return len(chunks)

    async def index_document(self, filename: str, content: bytes) -> int:
        documents = self.loader.load_bytes(filename, content)
        chunks = self._chunk(documents)
        await self.store.add_documents(chunks)
        return len(chunks)

    def _chunk(self, documents: list[Document]) -> list[Document]:
        chunks = []
        for doc in documents:
            for i, text in enumerate(self.splitter.split_text(doc.content)):
                chunk_id = hashlib.md5(f"{doc.source}:{i}:{text[:50]}".encode()).hexdigest()
                chunks.append(Document(content=text, metadata={**doc.metadata, "chunk_index": i, "chunk_id": chunk_id}))
        return chunks
