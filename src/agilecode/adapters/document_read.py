"""Read-only text extraction from PDF and DOCX files."""
from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

from agilecode.core.types import AgileCodeError

# Reject oversized DOCX archives before parsing their compressed XML payloads.
MAX_DOCX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_DOCX_ENTRIES = 10_000


def extract_pdf_text(data: bytes) -> str:
    """Extract selectable PDF text, with page boundaries visible to the model."""
    from pypdf import PdfReader

    try:
        document = PdfReader(BytesIO(data))
        if document.is_encrypted:
            raise AgileCodeError("Cannot read a password-protected PDF.")
        pages: list[str] = []
        has_text = False
        for number, page in enumerate(document.pages, start=1):
            body = (page.extract_text() or "").strip()
            has_text |= bool(body)
            pages.append(f"[Page {number}]\n{body}")
    except AgileCodeError:
        raise
    except Exception as exc:
        raise AgileCodeError("Cannot extract text from this PDF.") from exc

    if not has_text:
        raise AgileCodeError(
            "This PDF contains no extractable text. Scanned or image-only PDFs require OCR."
        )
    return "\n\n".join(pages) + "\n"


def extract_docx_text(data: bytes) -> str:
    """Extract Word paragraphs and tables in their original document order."""
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ModuleNotFoundError as exc:
        raise AgileCodeError(
            "DOCX reading requires python-docx in AgileCode's Python environment. "
            "Reinstall AgileCode to install its declared dependencies "
            "(for pipx: pipx reinstall agilecode)."
        ) from exc

    try:
        with ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            if (
                len(members) > MAX_DOCX_ENTRIES
                or sum(member.file_size for member in members) > MAX_DOCX_UNCOMPRESSED_BYTES
            ):
                raise AgileCodeError("DOCX exceeds the safe decompression limit.")

        document = Document(BytesIO(data))
        lines: list[str] = []
        for block in document.iter_inner_content():
            if isinstance(block, Paragraph):
                paragraph = block.text.strip()
                if paragraph:
                    lines.append(paragraph)
            elif isinstance(block, Table):
                for row in block.rows:
                    cells = [" ".join(cell.text.splitlines()).strip() for cell in row.cells]
                    lines.append(" | ".join(cells))
    except AgileCodeError:
        raise
    except Exception as exc:
        raise AgileCodeError("Cannot extract text from this DOCX.") from exc

    return "\n".join(lines) + ("\n" if lines else "")
