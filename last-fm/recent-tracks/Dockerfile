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
# check the runtime line. dependabot.yml ignores minor/major bumps of this
# image for that reason (this regressed twice: #36, #41) — bump both FROM
# lines together by hand when distroless moves to a new interpreter.
FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c AS builder
WORKDIR /app
COPY requirements.txt ./
# --target instead of a venv: the final stage has no python -m venv/activate
# machinery (no shell at all), just PYTHONPATH pointed at this directory.
RUN pip install --no-cache-dir --target=/app/deps -r requirements.txt

# Distroless, not python:3.13-slim: no pip/shell/package manager in the
# runtime image, so nothing to patch there that isn't actually used at
# runtime. :nonroot runs as a non-root UID. debian13 track, matching the
# now-playing image, for the same actively-rebuilt-base reasoning.
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:83aa8d4f74a4d7f7cf2d472054139bef71a927b76c680c0f2e1021d6b1d6d732
WORKDIR /app
ENV PYTHONPATH=/app/deps
COPY --from=builder /app/deps ./deps
COPY main.py ./
EXPOSE 8080
# Distroless's entrypoint is already `python3` — just pass the script.
CMD ["main.py"]
