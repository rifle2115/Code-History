FROM python:3.12-slim

# git does the blame and history work; it is the one system dependency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000 \
    ALLOW_LOCAL_REPOS=false

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend

# Run unprivileged; clones go to /tmp and the response cache to /app/workspace.
RUN useradd --create-home app && mkdir -p /app/workspace && chown app /app/workspace
USER app

EXPOSE 10000
# Shell form so $PORT (set by Render) is expanded. A graceful stop runs the
# shutdown hook that deletes temporary clones.
CMD uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT} --timeout-graceful-shutdown 10
