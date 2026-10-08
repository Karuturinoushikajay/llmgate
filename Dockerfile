FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config
COPY alembic ./alembic
COPY alembic.ini ./

RUN pip install .

RUN useradd --create-home --uid 10001 llmgate
USER llmgate

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["uvicorn", "llmgate.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
