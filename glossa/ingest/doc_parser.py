"""Parse an uploaded document (PDF/DOCX/PPTX/…) to text with LiteParse.

Used by ``upload``-mode sources. LiteParse (https://github.com/run-llama/liteparse)
is a local, no-API-key parser: PDFs parse natively, Office formats are converted
via LibreOffice, images via ImageMagick, and OCR uses Tesseract. Its output is
layout-preserved text, which is exactly the plain-text input the ingest extract
step consumes for every other source kind.

LiteParse is synchronous and infers the file type from the extension (Office
formats are zip-ambiguous from raw bytes), so we write the asset bytes to a temp
file with the original suffix and parse that path off the event loop.
"""

import asyncio
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from glossa.config import Settings


class DocumentParseError(RuntimeError):
    pass


def _parse_sync(data: bytes, filename: str, *, ocr_enabled: bool) -> str:
    try:
        from liteparse import LiteParse
    except ImportError as e:  # pragma: no cover - exercised only without the dep
        raise DocumentParseError(
            "upload-mode ingestion requires the 'liteparse' package (pip install liteparse)"
        ) from e

    suffix = Path(filename).suffix or ".pdf"
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        parser = LiteParse(ocr_enabled=ocr_enabled)
        result = parser.parse(tmp_path)
        return result.text or ""
    except DocumentParseError:
        raise
    except Exception as e:  # liteparse surfaces missing LibreOffice/Tesseract here
        raise DocumentParseError(f"failed to parse {filename!r}: {e}") from e
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def parse_asset_to_text(*, data: bytes, filename: str, settings: "Settings") -> str:
    """Parse the raw document ``data`` to text. Raises ``DocumentParseError``."""
    return await asyncio.to_thread(_parse_sync, data, filename, ocr_enabled=settings.liteparse_ocr_enabled)
