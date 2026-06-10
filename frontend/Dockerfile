# syntax=docker/dockerfile:1
# -----------------------------------------------------------------------------
# RAG Knowledge Base — frontend (Next.js 14)
#
# IMPORTANT (Next 14): NEXT_PUBLIC_* env vars are INLINED INTO THE BUNDLE AT
# BUILD TIME, not read at runtime. So NEXT_PUBLIC_API_BASE_URL must be present
# during `npm run build`. We accept it as a build arg (default points at the
# host-published backend) and promote it to an env var before building. To
# change the API URL you must rebuild the image (compose passes it as a build
# arg — see docker-compose.yml).
# -----------------------------------------------------------------------------

# ---- Stage 1: install dependencies ------------------------------------------
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ---- Stage 2: build ----------------------------------------------------------
FROM node:20-alpine AS builder
WORKDIR /app

# Build-time API base URL (inlined into the client bundle by Next 14).
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL

COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---- Stage 3: runtime --------------------------------------------------------
FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production

# Re-declare so the value is available (already baked into .next, kept for
# any server-side reads of process.env at runtime).
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL

# Copy the built app, public assets, config, and production deps.
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/package-lock.json ./package-lock.json
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/next.config.mjs ./next.config.mjs

EXPOSE 3000

# `next start` serves the production build on port 3000.
CMD ["npm", "run", "start"]
