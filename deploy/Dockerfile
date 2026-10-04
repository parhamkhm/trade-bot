# syntax=docker/dockerfile:1
#
# Image for the Tabdeal trade recorder (and, later, any other long-lived tbot
# service started the same way). Built and run on the Ubuntu VPS via
# deploy/docker-compose.yml; see deploy/SERVER_SETUP.md for the full walkthrough.
#
# Design notes (CLAUDE.md section 3.6 "Safety by default" / section 7 "Tech stack"):
#   - No secrets are baked into this image. `.env` is supplied at container run time
#     via `env_file:` in docker-compose.yml, never COPYed or ARGed here. The `recorder`
#     service specifically does NOT receive the repo-root `.env` at all (see
#     docker-compose.yml) — it only calls Tabdeal's public, unsigned endpoints
#     (`/trades`, `/depth`), so it has no business holding exchange credentials.
#   - Dependencies are installed with uv from the repo's committed uv.lock, so the
#     image gets exactly the versions CI tested, not whatever PyPI resolves today.
#   - The process runs as a non-root user.
#   - HEALTHCHECK below runs deploy/healthcheck.py, which parses the heartbeat file's
#     JSON (not just its mtime) and fails the container unhealthy both when the file
#     has gone stale AND when `consecutive_errors` shows every poll cycle is failing
#     even though the file is still being rewritten (fix-round finding M5 — a stale
#     mtime alone cannot see a recorder that is "alive but failing every poll").

FROM python:3.12-slim-bookworm AS base

# Pin uv to the version used to generate the committed uv.lock (see repo root
# `uv --version` on the maintainer's machine); bump both together.
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# --- dependency layer (cached unless pyproject.toml/uv.lock change) ---
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# --- application code ---
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY config/ ./config/
COPY README.md ./README.md

# Standalone, stdlib-only HEALTHCHECK script (see deploy/healthcheck.py's own docstring).
# Copied in separately from `src/` because it deliberately is NOT part of the `tbot`
# package — a HEALTHCHECK command should not depend on the project's own venv resolving
# cleanly to work.
COPY deploy/healthcheck.py ./healthcheck.py

RUN uv sync --locked --no-dev

# Non-root runtime user. UID/GID fixed (not auto-assigned) so the named volume's
# on-disk ownership is stable across image rebuilds.
RUN groupadd --gid 10001 tbot \
    && useradd --uid 10001 --gid tbot --create-home --shell /usr/sbin/nologin tbot \
    && mkdir -p /app/data \
    && chown -R tbot:tbot /app

USER tbot

# Where the recorder's SQLite DB, Parquet store and heartbeat file live inside the
# container; mounted as a named volume in docker-compose.yml (service: recorder).
VOLUME ["/app/data"]

# The recorder writes a heartbeat file at $TBOT_HEARTBEAT_FILE on every poll cycle
# (`tbot.data.tabdeal_recorder.write_heartbeat`, called from `process_once` — including on
# a cycle whose poll itself failed) with the fixed JSON contract:
#   {"last_poll_ts": "<ISO-8601 UTC>", "last_trade_id": int|null, "n_trades_total": int,
#    "consecutive_errors": int}
# deploy/healthcheck.py reads this file and fails unhealthy if `last_poll_ts` is older than
# $TBOT_HEARTBEAT_MAX_AGE_SECONDS *or* if `consecutive_errors` has reached
# $TBOT_HEARTBEAT_MAX_CONSECUTIVE_ERRORS (the process is alive but every poll is failing —
# the file would otherwise look fresh forever). If the recorder's actual heartbeat
# path/contract ever changes, update these ENV defaults (and the matching values in
# docker-compose.yml) to match.
ENV TBOT_HEARTBEAT_FILE=/app/data/tabdeal/heartbeat.json \
    TBOT_HEARTBEAT_MAX_AGE_SECONDS=180 \
    TBOT_HEARTBEAT_MAX_CONSECUTIVE_ERRORS=10

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "healthcheck.py"]

# scripts/record_tabdeal.py is the recorder entry point (`tbot.data.tabdeal_recorder`).
# Shell form (not exec form) so $TBOT_CONFIG — set in docker-compose.yml's `environment:`
# block, which config/<name>.yaml to load — is actually substituted into the `--config`
# flag rather than sitting in the container's environment unread (fix-round finding,
# minor #7: previously nothing read this variable at all). `exec` replaces the shell so
# PID 1 stays the Python process for signal handling (SIGTERM from `docker compose stop` /
# `docker compose down` reaches `scripts.record_tabdeal`'s own handler, not a wrapper
# shell); trailing `"$@"` forwards any extra args a future `docker compose run recorder
# ...` or `command:` override passes (e.g. `--once` for a one-shot debug run).
ENTRYPOINT ["sh", "-c", "exec python -m scripts.record_tabdeal --config \"${TBOT_CONFIG:-default}\" \"$@\"", "sh"]
