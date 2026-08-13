# Pinned by digest, not just the floating :24-slim tag: a floating tag makes
# builds nondeterministic (the same Dockerfile can start failing the scan
# purely because the tag moved, with no change on our side — hit this
# exact issue three times in a row before pinning). Dependabot's docker
# ecosystem entry (see ../../.github/dependabot.yml) proposes digest bumps
# on its own schedule, reviewed like any other dependency update.
FROM node:24-slim@sha256:3638d9a6fe4030bd716be989438248074489337ba3275657f93595428be4fc03 AS builder
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
FROM gcr.io/distroless/nodejs24-debian12:nonroot@sha256:14d42e2511532589a7c7e01a753667a74fcc96266e137e8125006b87b0c32d0a
WORKDIR /app
ENV NODE_ENV=production
# No runtime dependencies (built-in http + fetch only) — just the compiled output.
COPY --from=builder /app/dist ./dist
EXPOSE 8080
# Distroless's entrypoint is already `node` — just pass the script.
CMD ["dist/index.js"]
