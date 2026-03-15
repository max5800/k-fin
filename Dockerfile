FROM python:3.13-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first (layer caching)
COPY pyproject.toml ./

# Install dependencies (no dev extras in production)
RUN uv sync --no-dev --no-install-project

# Copy application code (no secrets, no .env — see .dockerignore)
COPY src/ src/
COPY scripts/ scripts/
COPY main.py ./

# Export directory — mount a volume here
RUN mkdir -p /data/exports

# Default: run the CSV export
ENTRYPOINT ["uv", "run", "python", "scripts/export_csv.py"]
CMD ["--output-dir", "/data/exports"]
