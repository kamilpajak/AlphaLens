# Multi-stage build: SvelteKit static SPA → nginx serving /usr/share/nginx/html.
# The pipeline container writes JSON into a bind-mount at /usr/share/nginx/html/data,
# so this image rarely changes — rebuild only when the UI itself changes.

# --- builder ---
FROM node:22-alpine AS builder
WORKDIR /app

# Match the project's pnpm version. corepack is bundled with node ≥16.
RUN corepack enable && corepack prepare pnpm@11.1.3 --activate

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

# --- runtime ---
FROM nginx:1-alpine AS runtime
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/build /usr/share/nginx/html

# Healthcheck — confirms nginx is up and the SPA shell loads.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O- http://127.0.0.1/ | grep -q ALPHALENS || exit 1

EXPOSE 80
