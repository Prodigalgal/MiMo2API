# syntax=docker/dockerfile:1.7

FROM node:24-bookworm-slim AS build
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
COPY package.json package-lock.json ./
# better-sqlite3 publishes a newer-GLIBC arm64 prebuild than Bookworm can load.
# Its normal install intentionally skips node-gyp when a prebuild is present,
# so force a source build and remove the prebuilds to select build/Release.
RUN npm ci \
    && cd node_modules/better-sqlite3 \
    && node /usr/local/lib/node_modules/npm/node_modules/node-gyp/bin/node-gyp.js rebuild --release --force_build=1 \
    && rm -rf prebuilds
COPY tsconfig.json vitest.config.ts ./
COPY src ./src
RUN npm run build && npm prune --omit=dev

FROM node:24-bookworm-slim
ENV NODE_ENV=production \
    PORT=8080 \
    MIMO2API_DATA_DIR=/var/lib/mimo2api \
    MIMO2API_DATABASE_FILE=/var/lib/mimo2api/mimo2api.sqlite \
    MIMO2API_CONFIG_FILE=/var/lib/mimo2api/config.json
WORKDIR /app
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY package.json config.example.json ./
COPY web ./web
RUN groupadd --gid 10001 mimo2api \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin mimo2api \
    && mkdir -p /var/lib/mimo2api \
    && chown -R 10001:10001 /app /var/lib/mimo2api
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:8080/healthz').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"
CMD ["node", "dist/index.js"]
