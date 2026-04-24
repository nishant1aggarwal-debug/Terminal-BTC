FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts

# Install app + Postgres driver so a DATABASE_URL like
# postgresql+psycopg://... works without rebuilding.
RUN pip install --no-cache-dir -e ".[postgres]"

RUN mkdir -p /app/data
ENV DATABASE_URL=sqlite:////app/data/terminal_btc.db
ENV LOG_LEVEL=INFO
ENV PAPER_MODE=true
ENV SIGNAL_MODE=rules
ENV DATA_SOURCE=bybit

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
