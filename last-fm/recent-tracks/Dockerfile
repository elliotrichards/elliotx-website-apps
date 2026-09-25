# Pinned by digest, not just the floating :3.13-slim tag — see the
# now-playing Dockerfile for why (nondeterministic scans otherwise).
# Dependabot's docker ecosystem entry (../../.github/dependabot.yml)
# proposes digest bumps on its own schedule.
#
# Major/minor MUST match the distroless runtime image's bundled interpreter
# below (currently 3.13 — `docker run --entrypoint python3 <that image>
# --version`), not just its Debian track. A prior 3.13->3.14 digest bump
# here, with the runtime still on 3.13, shipped cp314-only compiled
# extensions (cffi's _cffi_backend, pulled in via cryptography) into an
# interpreter that can't load them — ModuleNotFoundError at import time,
# so the container never opens the health-check port and Cloud Run's
# deploy fails. Dependabot bumps this line on its own and won't know to
# check the runtime line, so verify that match by hand before merging one.
FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0 AS builder
WORKDIR /app
COPY requirements.txt ./
# --target instead of a venv: the final stage has no python -m venv/activate
# machinery (no shell at all), just PYTHONPATH pointed at this directory.
RUN pip install --no-cache-dir --target=/app/deps -r requirements.txt

# Distroless, not python:3.13-slim: no pip/shell/package manager in the
# runtime image, so nothing to patch there that isn't actually used at
# runtime. :nonroot runs as a non-root UID. debian13 track, matching the
# now-playing image, for the same actively-rebuilt-base reasoning.
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:8ee214843129f43e2ebf5e0ca9f2e4e6d8292143d1b8a6787f169b5898578884
WORKDIR /app
ENV PYTHONPATH=/app/deps
COPY --from=builder /app/deps ./deps
COPY main.py ./
EXPOSE 8080
# Distroless's entrypoint is already `python3` — just pass the script.
CMD ["main.py"]
