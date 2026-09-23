# MAPEX — one Python service on Railway. Runs as root (Railway volumes); no railway.json (deprecated).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY mapex ./mapex
# shell form so $PORT (set by Railway) expands; /health never depends on cTrader
CMD /app/.venv/bin/python -m mapex.main
