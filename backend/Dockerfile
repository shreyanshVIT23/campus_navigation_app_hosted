# syntax=docker/dockerfile:1

# ============================================================
# Builder
# ============================================================

FROM python:3.10.19-slim-trixie AS builder

# Pin uv to a specific version.
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /bin/

WORKDIR /app

# Use Python from the base image rather than downloading
# another Python version through uv.
ENV UV_PYTHON_DOWNLOADS=0

# Do not install development dependencies.
ENV UV_NO_DEV=1

# uv cache and project environment are on different filesystems.
ENV UV_LINK_MODE=copy

# Compile Python bytecode for production.
ENV UV_COMPILE_BYTECODE=1

# ============================================================
# Dependencies
# ============================================================

# Copy dependency metadata first so Docker can cache this layer.
COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync \
  --locked \
  --no-install-project \
  --no-editable

# ============================================================
# Application
# ============================================================

COPY server ./server

# ============================================================
# Install project
# ============================================================

RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync \
  --locked \
  --no-editable


WORKDIR /app/server

# Data paths (relative to server/). .env files stay out of the image, so the
# import check below needs them; at runtime compose env_file can override them.
ENV DB_PATH=DB/
ENV DB_EVENT=DB/event1.db
ENV DB_AUTH_PATH=DB/user_auth.db
ENV DB_TEACHER_DATA=DB/chatbot2.db
ENV EXCEL_TEACHER_PATH=Teacher_Data/faculty.xlsx
ENV FLOOR_MAPS_DIR=Floor_Maps
ENV ASSETS_DIR=Assets
ENV AUDIO_DIR=Chatbot/temp_audio

RUN /app/.venv/bin/python -c "import app; print(\"IMPORT OK\")"

# ============================================================
# Runtime
# ============================================================

FROM python:3.10.19-slim-trixie AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH="/app/.venv/bin:$PATH"

# Data paths (relative to server/); secrets come from compose env_file.
ENV DB_PATH=DB/
ENV DB_EVENT=DB/event1.db
ENV DB_AUTH_PATH=DB/user_auth.db
ENV DB_TEACHER_DATA=DB/chatbot2.db
ENV EXCEL_TEACHER_PATH=Teacher_Data/faculty.xlsx
ENV FLOOR_MAPS_DIR=Floor_Maps
ENV ASSETS_DIR=Assets
ENV AUDIO_DIR=Chatbot/temp_audio

# Create an unprivileged user.
RUN useradd \
  --create-home \
  --shell /usr/sbin/nologin \
  appuser

# Copy application + virtual environment from builder.
COPY --from=builder --chown=appuser:appuser /app /app

USER appuser

WORKDIR /app/server

EXPOSE 8000

# SWAG serves the API under /api and strips the prefix before forwarding;
# root_path lets /api/docs find /api/openapi.json. Only SWAG reaches this
# container (no published port), so its forwarded headers are trusted.
# One worker: route maps live in this process's memory (Utils/route_maps).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--root-path", "/api", "--forwarded-allow-ips", "*"]
