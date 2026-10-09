FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

ARG INSTALL_EMBEDDINGS=false
ARG INSTALL_OCR=false
COPY requirements.txt requirements-embeddings.txt requirements-ocr.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN if [ "$INSTALL_EMBEDDINGS" = "true" ]; then       pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu       && pip install --no-cache-dir -r requirements-embeddings.txt; fi
RUN if [ "$INSTALL_OCR" = "true" ]; then       apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*       && pip install --no-cache-dir -r requirements-ocr.txt; fi

COPY . .
ENV HF_HOME=/home/eduos/.cache/huggingface
RUN chmod +x docker-entrypoint.sh \
    && useradd --create-home --uid 10001 eduos \
    && mkdir -p /app/storage "$HF_HOME" && chown -R eduos /app/storage /home/eduos
USER eduos

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).status == 200 else 1)"
ENTRYPOINT ["./docker-entrypoint.sh"]
