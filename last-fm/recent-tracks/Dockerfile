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
FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb AS builder
WORKDIR /app
COPY requirements.txt ./
# --target instead of a venv: the final stage has no python -m venv/activate
# machinery (no shell at all), just PYTHONPATH pointed at this directory.
RUN pip install --no-cache-dir --target=/app/deps -r requirements.txt

# Distroless, not python:3.13-slim: no pip/shell/package manager in the
# runtime image, so nothing to patch there that isn't actually used at
# runtime. :nonroot runs as a non-root UID. debian13 track, matching the
# now-playing image, for the same actively-rebuilt-base reasoning.
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:64e54c4b1200da244d80181d98e86fc01d73c8101f107d5baf472152d8e119a2
WORKDIR /app
ENV PYTHONPATH=/app/deps
# Without this, print() is block-buffered (stdout isn't a TTY), so the
# "run failed:" lines in main.py never reach Cloud Logging.
ENV PYTHONUNBUFFERED=1
COPY --from=builder /app/deps ./deps
COPY main.py ./
EXPOSE 8080
# Distroless's entrypoint is already `python3` — just pass the script.
CMD ["main.py"]
