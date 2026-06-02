# syntax=docker/dockerfile:1.7
# TurboVid — bulkvid-runner image
# Plan: _plans/2026-06-02-aporia-bulk-video-tool.md §5, §13 phase 7

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps. NB: ffmpeg is INTENTIONALLY not bundled — we use Rendi.dev.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
 && rm -rf /var/lib/apt/lists/*

# Install Python deps in a separate layer so application changes don't bust it.
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --upgrade pip \
 && pip install -e .

# Default data dir mirrors the production /data/bulkvid layout.
RUN mkdir -p /data/bulkvid/logs
ENV BULKVID_DATA_DIR=/data/bulkvid

EXPOSE 8788

# tini handles PID 1 + zombie reaping. uvicorn does the rest.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "bulkvid.main:app", "--host", "0.0.0.0", "--port", "8788"]
