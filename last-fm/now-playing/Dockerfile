# Pinned by digest, not just the floating :24-slim tag: a floating tag makes
# builds nondeterministic (the same Dockerfile can start failing the scan
# purely because the tag moved, with no change on our side — hit this
# exact issue three times in a row before pinning). Dependabot's docker
# ecosystem entry (see ../../.github/dependabot.yml) proposes digest bumps
# on its own schedule, reviewed like any other dependency update.
FROM node:25-slim@sha256:81db02c4b671288a03915da9534dbd54f96d0e7c24d80ccc54f5b36b2e684370 AS builder
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

# Distroless, not node:24-slim: no npm/shell/package manager in the runtime
# image at all. node:24-slim still bundles npm (and npm's own dependencies,
# e.g. tar/brace-expansion) whether or not this stage ever runs it, so
# Trivy kept flagging CVEs in files that just sit there unused. Distroless
# has none of that — smaller image, less attack surface, nothing to patch
# that isn't actually needed at runtime. :nonroot runs as a non-root UID.
#
# debian13, not debian12: the debian12 track's nonroot digest hadn't been
# rebuilt since Feb 2026, so 21 libssl3/libc6 CVEs disclosed since then
# (including one critical) had no patched image to bump to. debian13 is
# the actively-rebuilt track (last rebuild: days old) and carries none of
# those CVE IDs.
FROM gcr.io/distroless/nodejs24-debian13:nonroot@sha256:ffab599740d4aaa66029d02b9e6d3de4f622fefb7410081c5ef69c86430f364d
WORKDIR /app
ENV NODE_ENV=production
# No runtime dependencies (built-in http + fetch only) — just the compiled output.
COPY --from=builder /app/dist ./dist
EXPOSE 8080
# Distroless's entrypoint is already `node` — just pass the script.
CMD ["dist/index.js"]
