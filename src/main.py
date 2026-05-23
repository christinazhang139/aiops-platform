from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from prometheus_client import make_asgi_app
from src.config.settings import get_settings
from src.api.routes import api_router

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    print(f"Starting AIOps Platform [provider={settings.llm_provider}, model={settings.llm_model}]")
    try:
        from src.rag.vector_store import get_vector_store
        from src.rag.indexer import KnowledgeIndexer
        store = get_vector_store()
        if store.collection.count() == 0:
            print("Vector store empty, auto-indexing knowledge base...")
            indexer = KnowledgeIndexer()
            count = await indexer.index_all()
            print(f"Indexed {count} document chunks")
        else:
            print(f"Vector store has {store.collection.count()} chunks")
    except Exception as e:
        print(f"Auto-indexing skipped: {e}")
    yield
    print("Shutting down AIOps Platform")

def create_app() -> FastAPI:
    app = FastAPI(title="AIOps Platform", description="AI-Powered Operations Platform", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.mount("/metrics", make_asgi_app())
    app.include_router(api_router, prefix="/api/v1")

    @app.get("/")
    async def root():
        return FileResponse("src/static/index.html")

    @app.get("/dashboard")
    async def dashboard():
        return FileResponse("src/static/dashboard.html")

    return app

app = create_app()
