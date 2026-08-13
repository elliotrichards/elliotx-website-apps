FROM node:24-slim AS builder
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
FROM gcr.io/distroless/nodejs24-debian12:nonroot
WORKDIR /app
ENV NODE_ENV=production
# No runtime dependencies (built-in http + fetch only) — just the compiled output.
COPY --from=builder /app/dist ./dist
EXPOSE 8080
# Distroless's entrypoint is already `node` — just pass the script.
CMD ["dist/index.js"]
