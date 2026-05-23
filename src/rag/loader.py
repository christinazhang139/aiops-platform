from pathlib import Path
from dataclasses import dataclass, field

@dataclass
class Document:
    content: str
    metadata: dict = field(default_factory=dict)

    @property
    def source(self) -> str:
        return self.metadata.get("source", "unknown")

class DocumentLoader:
    SUPPORTED = {".md", ".txt", ".yaml", ".yml"}

    def __init__(self, base_dir: str = "./knowledge_base"):
        self.base_dir = Path(base_dir)

    def load_all(self) -> list[Document]:
        docs = []
        if not self.base_dir.exists():
            return docs
        for f in self.base_dir.rglob("*"):
            if f.suffix in self.SUPPORTED:
                docs.extend(self.load_file(f))
        return docs

    def load_file(self, path: Path) -> list[Document]:
        try:
            content = path.read_text(encoding="utf-8")
            rel = path.relative_to(self.base_dir)
            meta = {"source": str(rel), "filename": path.name, "category": rel.parts[0] if len(rel.parts) > 1 else "general"}
            if path.suffix == ".md":
                return self._split_by_headers(content, meta)
            return [Document(content=content, metadata=meta)]
        except Exception:
            return []

    def _split_by_headers(self, content: str, base_meta: dict) -> list[Document]:
        sections, current, header = [], "", ""
        for line in content.split("\n"):
            if line.startswith("## "):
                if current.strip():
                    sections.append(Document(content=current.strip(), metadata={**base_meta, "section": header}))
                header = line[3:].strip()
                current = line + "\n"
            else:
                current += line + "\n"
        if current.strip():
            sections.append(Document(content=current.strip(), metadata={**base_meta, "section": header or "intro"}))
        return sections or [Document(content=content, metadata=base_meta)]

    def load_bytes(self, filename: str, content: bytes) -> list[Document]:
        text = content.decode("utf-8", errors="replace")
        return [Document(content=text, metadata={"source": filename, "category": "uploaded"})]
