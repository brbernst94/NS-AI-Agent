"""Document ingestion pipeline: PDF, Word, CSV, URL, and plain text."""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Chunk size in approximate characters (1 token ≈ 4 chars, ~500 tokens ≈ 2000 chars)
_CHUNK_SIZE = 2000
_CHUNK_OVERLAP = 200


def _chunk_text(text: str, chunk_size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """
    Split text into overlapping chunks.

    Tries to split on paragraph boundaries first; falls back to hard split.
    """
    text = re.sub(r"\n{3,}", "\n\n", text)  # Collapse excessive blank lines
    text = text.strip()
    if not text:
        return []

    # Try paragraph-based splitting
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= chunk_size:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
            # If single paragraph exceeds chunk_size, hard-split it
            if len(para) > chunk_size:
                for i in range(0, len(para), chunk_size - overlap):
                    sub = para[i : i + chunk_size]
                    if sub.strip():
                        chunks.append(sub.strip())
                current = ""
            else:
                current = para

    if current:
        chunks.append(current)

    # Apply overlap: prepend tail of previous chunk
    if overlap > 0 and len(chunks) > 1:
        overlapped: list[str] = [chunks[0]]
        for i in range(1, len(chunks)):
            tail = chunks[i - 1][-overlap:]
            overlapped.append((tail + " " + chunks[i]).strip())
        return overlapped

    return chunks


class DocumentIngester:
    """Ingest documents into the KnowledgeIndex from various formats."""

    def __init__(self, knowledge_index: Any) -> None:
        self.index = knowledge_index

    # ------------------------------------------------------------------
    # PDF
    # ------------------------------------------------------------------

    def ingest_pdf(self, file_path: str | Path) -> int:
        """Extract text from a PDF page by page, chunk, and add to index."""
        file_path = Path(file_path)
        try:
            from pypdf import PdfReader
        except ImportError:
            logger.error("pypdf is not installed. Run: pip install pypdf")
            return 0

        try:
            reader = PdfReader(str(file_path))
        except Exception as exc:
            logger.error("Failed to read PDF '%s': %s", file_path, exc)
            return 0

        docs: list[dict[str, Any]] = []
        for page_num, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if not text.strip():
                continue
            chunks = _chunk_text(text)
            for chunk_idx, chunk in enumerate(chunks):
                docs.append(
                    {
                        "text": chunk,
                        "source": file_path.name,
                        "metadata": {
                            "page": page_num,
                            "chunk_index": chunk_idx,
                            "file_type": "pdf",
                            "file_path": str(file_path),
                        },
                    }
                )

        added = self.index.add_documents(docs)
        logger.info("Ingested PDF '%s': %d chunks added.", file_path.name, added)
        return added

    # ------------------------------------------------------------------
    # Word (.docx)
    # ------------------------------------------------------------------

    def ingest_word(self, file_path: str | Path) -> int:
        """Extract paragraphs from a .docx file, chunk, and add to index."""
        file_path = Path(file_path)
        try:
            from docx import Document
        except ImportError:
            logger.error("python-docx is not installed. Run: pip install python-docx")
            return 0

        try:
            doc = Document(str(file_path))
        except Exception as exc:
            logger.error("Failed to read Word document '%s': %s", file_path, exc)
            return 0

        # Collect all paragraph text
        full_text = "\n\n".join(
            p.text.strip() for p in doc.paragraphs if p.text.strip()
        )

        # Also extract text from tables
        table_texts: list[str] = []
        for table in doc.tables:
            rows: list[str] = []
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                table_texts.append("\n".join(rows))

        if table_texts:
            full_text += "\n\n" + "\n\n".join(table_texts)

        if not full_text.strip():
            logger.warning("No text extracted from Word document '%s'.", file_path.name)
            return 0

        chunks = _chunk_text(full_text)
        docs = [
            {
                "text": chunk,
                "source": file_path.name,
                "metadata": {
                    "chunk_index": i,
                    "file_type": "docx",
                    "file_path": str(file_path),
                },
            }
            for i, chunk in enumerate(chunks)
        ]

        added = self.index.add_documents(docs)
        logger.info("Ingested Word doc '%s': %d chunks added.", file_path.name, added)
        return added

    # ------------------------------------------------------------------
    # CSV / Excel
    # ------------------------------------------------------------------

    def ingest_csv(self, file_path: str | Path) -> int:
        """
        Treat a CSV as structured documentation.
        Converts each row into a descriptive text chunk and adds to index.
        """
        file_path = Path(file_path)
        try:
            import pandas as pd
        except ImportError:
            logger.error("pandas is not installed.")
            return 0

        try:
            if file_path.suffix.lower() in (".xlsx", ".xls"):
                df = pd.read_excel(str(file_path), dtype=str)
            else:
                df = pd.read_csv(str(file_path), dtype=str)
        except Exception as exc:
            logger.error("Failed to read CSV/Excel '%s': %s", file_path, exc)
            return 0

        df = df.fillna("")
        columns = list(df.columns)

        docs: list[dict[str, Any]] = []
        # Build a header description chunk
        header_chunk = (
            f"File: {file_path.name}\n"
            f"Columns ({len(columns)}): {', '.join(columns)}\n"
            f"Total rows: {len(df)}"
        )
        docs.append(
            {
                "text": header_chunk,
                "source": file_path.name,
                "metadata": {"chunk_type": "header", "file_type": "csv"},
            }
        )

        # Convert rows to text in batches of 20
        batch_size = 20
        for batch_start in range(0, len(df), batch_size):
            batch = df.iloc[batch_start : batch_start + batch_size]
            lines = [f"Records {batch_start + 1}–{batch_start + len(batch)} from {file_path.name}:"]
            for _, row in batch.iterrows():
                parts = [f"{col}={val}" for col, val in row.items() if val]
                if parts:
                    lines.append("  { " + ", ".join(parts) + " }")
            text = "\n".join(lines)
            docs.append(
                {
                    "text": text,
                    "source": file_path.name,
                    "metadata": {
                        "chunk_type": "rows",
                        "row_start": batch_start,
                        "file_type": "csv",
                    },
                }
            )

        added = self.index.add_documents(docs)
        logger.info("Ingested CSV '%s': %d chunks added.", file_path.name, added)
        return added

    # ------------------------------------------------------------------
    # URL
    # ------------------------------------------------------------------

    def ingest_url(self, url: str) -> int:
        """Fetch a URL, parse HTML with BeautifulSoup, chunk text, add to index."""
        try:
            import requests
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error("requests or beautifulsoup4 not installed.")
            return 0

        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": "NS-AI-Agent/1.0"})
            resp.raise_for_status()
        except Exception as exc:
            logger.error("Failed to fetch URL '%s': %s", url, exc)
            return 0

        try:
            soup = BeautifulSoup(resp.content, "html.parser")

            # Remove boilerplate tags
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
                tag.decompose()

            # Try to get main content
            main = soup.find("main") or soup.find("article") or soup.find("div", {"id": "content"})
            target = main if main else soup

            # Extract text with spacing preserved
            text = target.get_text(separator="\n", strip=True)
        except Exception as exc:
            logger.error("Failed to parse HTML from '%s': %s", url, exc)
            return 0

        if not text.strip():
            logger.warning("No text extracted from URL '%s'.", url)
            return 0

        source_name = url[:100]  # Use URL as source name (truncated)
        chunks = _chunk_text(text)
        docs = [
            {
                "text": chunk,
                "source": source_name,
                "metadata": {
                    "chunk_index": i,
                    "file_type": "url",
                    "url": url,
                },
            }
            for i, chunk in enumerate(chunks)
        ]

        added = self.index.add_documents(docs)
        logger.info("Ingested URL '%s': %d chunks added.", url, added)
        return added

    # ------------------------------------------------------------------
    # Plain text
    # ------------------------------------------------------------------

    def ingest_text(self, text: str, source_name: str) -> int:
        """Directly ingest plain text under a given source name."""
        if not text.strip():
            return 0
        chunks = _chunk_text(text)
        docs = [
            {
                "text": chunk,
                "source": source_name,
                "metadata": {
                    "chunk_index": i,
                    "file_type": "text",
                },
            }
            for i, chunk in enumerate(chunks)
        ]
        added = self.index.add_documents(docs)
        logger.info("Ingested text '%s': %d chunks added.", source_name, added)
        return added

    # ------------------------------------------------------------------
    # File dispatcher
    # ------------------------------------------------------------------

    def ingest_file(self, file_path: str | Path, content: bytes | None = None) -> int:
        """
        Ingest a file based on its extension.
        If `content` bytes are provided, write to a temp file first.
        """
        file_path = Path(file_path)
        ext = file_path.suffix.lower()

        if content is not None:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(content)
                tmp_path = Path(tmp.name)
            try:
                return self._dispatch(tmp_path, original_name=file_path.name)
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            return self._dispatch(file_path, original_name=file_path.name)

    def _dispatch(self, file_path: Path, original_name: str) -> int:
        ext = file_path.suffix.lower()
        if ext == ".pdf":
            result = self.ingest_pdf(file_path)
        elif ext in (".docx", ".doc"):
            result = self.ingest_word(file_path)
        elif ext in (".csv", ".xlsx", ".xls"):
            result = self.ingest_csv(file_path)
        elif ext in (".txt", ".md", ".rst"):
            text = file_path.read_text(encoding="utf-8", errors="replace")
            result = self.ingest_text(text, original_name)
        else:
            logger.warning("Unsupported file type: %s", ext)
            return 0
        return result
