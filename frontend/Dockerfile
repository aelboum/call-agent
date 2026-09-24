# Production image for the frontend (Phase 2.18, Workstream I): builds the
# static production bundle (`vite build`, verified separately in Phase 2.17
# and again this phase) and serves it from nginx -- never the Vite dev
# server, which this Dockerfile has no stage that even installs.

FROM node:22-alpine AS builder
WORKDIR /app

# package.json + package-lock.json first so `npm ci` only re-runs when a
# dependency actually changes, not on every source edit.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# `nginxinc/nginx-unprivileged`: the official nginx image, rebuilt to run as
# a non-root user and listen on an unprivileged port (8080) by default --
# avoids the plain `nginx` image's own root-then-drop-privileges startup
# entirely (Phase 2.18 brief, Workstream C: "non-root execution where
# practical").
FROM nginxinc/nginx-unprivileged:1.27-alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template

ENV API_UPSTREAM=http://api:8000

EXPOSE 8080
