from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = ""
    embedding_model: str = "text-embedding-3-small"
    chroma_persist_dir: str = "./data/chroma"
    chunk_size: int = 1000
    chunk_overlap: int = 200
    knowledge_base_dir: str = "./knowledge_base"
    redis_url: str = "redis://localhost:6379/0"
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

def build_llm():
    settings = get_settings()
    from langchain_openai import ChatOpenAI
    kwargs = {
        "model": settings.llm_model,
        "api_key": settings.openai_api_key,
        "temperature": 0,
    }
    if settings.llm_base_url:
        kwargs["base_url"] = settings.llm_base_url
    return ChatOpenAI(**kwargs)

@lru_cache
def get_settings() -> Settings:
    return Settings()
