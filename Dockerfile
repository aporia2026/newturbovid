# TurboVid container image — for HuggingFace Spaces and any Docker host.
#
# Runs BOTH the FastAPI web app (uvicorn) AND the always-on worker
# (bulkvid.worker) inside one container under supervisord, sharing the
# same Turso libSQL connection. Plan:
#   _plans/2026-06-04-migrate-to-hf-spaces-turso.md
#
# HuggingFace Spaces conventions:
#   - Listen on port 7860 (Spaces exposes this externally as the Space URL).
#   - Writable filesystem is /tmp ONLY. Everything stateful goes either to
#     /tmp (ephemeral logs, the local SQLite replica) or to Turso (durable).
#   - Secrets come from "Space Secrets" (env vars), not files.

FROM python:3.12-slim

# System deps:
#   supervisor — runs uvicorn + worker side by side.
#   ca-certificates + curl — TLS to vendor APIs, downloads in build only.
#   build-essential — only kept around long enough to build libsql's
#     pyo3 native extension if the wheel index doesn't have a match for
#     the slim image's libc; removed after pip install to keep the image
#     small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        supervisor \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the dependency manifest first so Docker can cache the pip layer
# across code-only edits.
COPY pyproject.toml README.md ./
COPY src ./src

# Install bulkvid + the turso extra in one layer. ``--no-cache-dir`` keeps
# the image lean. The ``[turso]`` extra pulls in the libsql Python
# bindings — see pyproject.toml.
RUN pip install --no-cache-dir -e ".[turso]"

# Supervisord config: one stanza per process. Both autorestart so a
# crashed worker doesn't quietly stay dead.
COPY supervisord.conf /etc/supervisord.conf

# HF Spaces convention.
EXPOSE 7860

# /tmp is the only writable path on HF Spaces — point the data dir there
# so the local SQLite replica + per-job log files have somewhere to live.
# Overridable per-deploy via the BULKVID_DATA_DIR env var.
ENV BULKVID_DATA_DIR=/tmp/data
ENV BULKVID_PORT=7860
ENV PYTHONUNBUFFERED=1

# Heredoc would be cleaner here but Docker's BuildKit version on HF is
# unpredictable; a plain CMD is the safe choice.
CMD ["supervisord", "-c", "/etc/supervisord.conf", "--nodaemon"]
