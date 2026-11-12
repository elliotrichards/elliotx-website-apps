# Same base images, digests and builder/runtime python pairing as
# ../../last-fm/recent-tracks/Dockerfile — see that file for the full
# reasoning. Short version: the builder's python major/minor MUST match the
# distroless runtime's bundled interpreter (3.13), or compiled extensions
# built for one won't load in the other. dependabot.yml only allows digest/
# patch bumps of the builder for that reason; bump both FROM lines together
# by hand when distroless moves to a new interpreter.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS builder
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --target=/app/deps -r requirements.txt

FROM gcr.io/distroless/python3-debian13:nonroot@sha256:c3ed2b61f3c4e410685565ceb3c818197b552832521b39d53eca5d2d9d0dc1e1
WORKDIR /app
ENV PYTHONPATH=/app/deps
# Without this, print() is block-buffered (stdout isn't a TTY) and log lines
# only reach Cloud Logging when the buffer fills, i.e. effectively never for
# a short daily run.
ENV PYTHONUNBUFFERED=1
COPY --from=builder /app/deps ./deps
# src/ is flattened into /app, so main.py's sibling imports (sheet, tmdb, …)
# resolve from the script's own directory, same as locally.
COPY src/ ./
EXPOSE 8080
CMD ["main.py"]
