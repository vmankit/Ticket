# Deploy with this (rather than the native Python runtime) when scanned-ticket
# OCR is needed: tesseract is a system binary that pip cannot install.
FROM python:3.11-slim

# tesseract-ocr powers scanned-ticket upload; the app runs without it, but
# those uploads then fall back to manual entry.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5000
EXPOSE 5000

# SECRET_KEY must be supplied by the environment; the app refuses to boot in
# production without it.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
