# syntax=docker/dockerfile:1.7
# Builds the React UI and serves it with nginx. API and token URLs are baked in
# at build time (Vite), and default to the ports docker compose publishes.
FROM node:22-alpine AS build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY ui/ ./
ARG VITE_API_URL=http://localhost:8000
ARG VITE_TOKEN_URL=http://localhost:8090/token
ENV VITE_API_URL=${VITE_API_URL} VITE_TOKEN_URL=${VITE_TOKEN_URL}
RUN npm run build

FROM nginx:1.27-alpine
COPY infra/docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /ui/dist /usr/share/nginx/html
EXPOSE 80
