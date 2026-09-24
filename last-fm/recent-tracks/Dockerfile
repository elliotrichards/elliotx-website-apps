# Pinned by digest, not just the floating :3.13-slim tag — see the
# now-playing Dockerfile for why (nondeterministic scans otherwise).
# Dependabot's docker ecosystem entry (../../.github/dependabot.yml)
# proposes digest bumps on its own schedule.
FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2 AS builder
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
