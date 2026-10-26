# Same base images, digests and builder/runtime python pairing as
# ../../last-fm/recent-tracks/Dockerfile — see that file for the full
# reasoning. Short version: the builder's python major/minor MUST match the
# distroless runtime's bundled interpreter (3.13), or compiled extensions
# built for one won't load in the other. dependabot.yml only allows digest/
# patch bumps of the builder for that reason; bump both FROM lines together
# by hand when distroless moves to a new interpreter.
FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c AS builder
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --target=/app/deps -r requirements.txt

FROM gcr.io/distroless/python3-debian13:nonroot@sha256:774595d652a294b54c9bd575b2d9fdd1a4b47547dc17b8bfa4c0e953c64855b3
WORKDIR /app
ENV PYTHONPATH=/app/deps
COPY --from=builder /app/deps ./deps
# src/ is flattened into /app, so main.py's sibling imports (sheet, tmdb, …)
# resolve from the script's own directory, same as locally.
COPY src/ ./
EXPOSE 8080
CMD ["main.py"]
