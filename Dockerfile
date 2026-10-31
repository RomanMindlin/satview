FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv

# dependencies first, so code changes don't reinstall them
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project \
    && rm -rf /root/.cache

COPY README.md telegram_sender.py ./
COPY satview/ ./satview/
RUN uv sync --locked \
    && rm -rf /root/.cache

COPY crontab /etc/cron.d/satview
RUN chmod 0644 /etc/cron.d/satview

COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

# config.yaml and .env are mounted at /app; output/ and cache/ must outlive the container
VOLUME ["/app/output", "/app/cache"]

ENTRYPOINT ["./docker-entrypoint.sh"]
