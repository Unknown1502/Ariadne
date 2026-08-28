# Ariadne investigation console.
#
# The console holds no state of its own and never talks to Firestore, Cloud SQL, or Pub/Sub
# directly - it only reads the ledger through the API (see src/api.ts: BASE = "", every call
# is a relative path). Serving it from a different origin than the API would mean either
# CORS or an absolute API URL baked into the build; instead nginx proxies /api and /health
# to the real backend, so the browser only ever talks to one origin, exactly like the Vite
# dev server's proxy config does locally.

FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

# Cloud Run injects PORT; nginx's own template mechanism (nginx:*-alpine ships
# `docker-entrypoint.d/20-envsubst-on-templates.sh`, run automatically on container start)
# substitutes PORT, API_ORIGIN, and API_HOST into the real config before nginx starts - no
# custom entrypoint script needed.
#
# API_HOST is the bare hostname API_ORIGIN points at, kept separate because the proxied
# request's Host header must be the API's own hostname, never the console's - see the
# comment in nginx.conf.template for what went wrong the one time this used $host instead.
ENV PORT=8080
ENV API_ORIGIN=https://ariadne-api-uhcrowxnsq-el.a.run.app
ENV API_HOST=ariadne-api-uhcrowxnsq-el.a.run.app
EXPOSE 8080
