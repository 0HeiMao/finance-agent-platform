from pathlib import Path


def extract_text(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in {".txt", ".md", ".csv"}:
        raw = Path(path).read_bytes()
        # 优先 UTF-8，兼容历史中文财务系统常见的 GB18030；严格解码避免静默产生乱码。
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError("文本文件不是有效的 UTF-8 或 GB18030 编码")
    if suffix == ".pdf":
        import fitz
        document = fitz.open(path)
        return "\n".join(page.get_text() for page in document)
    if suffix == ".docx":
        from docx import Document
        return "\n".join(paragraph.text for paragraph in Document(path).paragraphs)
    raise ValueError(f"暂不支持的文件类型: {suffix}")


def split_text(text: str, chunk_size: int = 800, overlap: int = 120) -> list[str]:
    normalized = " ".join(text.split())
    if not normalized:
        return []
    chunks = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + chunk_size)
        chunks.append(normalized[start:end])
        if end == len(normalized):
            break
        start = end - overlap
    return chunks
