FROM python:3.12-slim

WORKDIR /app

# System dependencies for LiteParse document parsing (upload-mode sources):
#   - libreoffice: Office formats (.docx/.pptx/.xlsx/.odt/…) -> PDF
#   - imagemagick: image inputs (.png/.jpg/.tiff/…)
#   - tesseract-ocr: OCR for scanned documents
# PDFs parse without these; drop this layer if you only need PDF uploads and
# want a smaller image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice imagemagick tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY glossa/ ./glossa/

EXPOSE 8200

CMD ["sh", "-c", "uvicorn glossa.main:app --host 0.0.0.0 --port ${PORT:-8200}"]
