FROM node:22-alpine AS frontend

WORKDIR /build/web-vue
COPY web-vue/package.json web-vue/package-lock.json ./
RUN npm ci
COPY web-vue/ ./
RUN npm run build


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .
COPY --from=frontend /build/web-vue/dist /app/web-vue/dist

RUN mkdir -p /app/workspace/assets /app/.agent/sessions /app/.agent/production-runs

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD curl --fail --silent http://127.0.0.1:8000/api/health >/dev/null || exit 1

CMD ["python", "run_full.py", "--host", "0.0.0.0", "--port", "8000"]
