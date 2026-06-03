FROM node:20-alpine

WORKDIR /app

ARG NEXT_PUBLIC_API_URL=""
ARG NEXT_PUBLIC_WS_URL=""

ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_WS_URL=$NEXT_PUBLIC_WS_URL
# Cap Node heap so `next build` doesn't OOM on small EC2 instances
# (the v1 frontend uses the default ~1.4GB which is fine; v2 has more
# routes and was OOM-killing during the first deploy attempt).
ENV NODE_OPTIONS="--max-old-space-size=1024"

COPY package*.json ./
# --no-cache fresh install every deploy → harden against npm-registry blips
# with fetch-retries + a fallback retry (see frontend/Dockerfile note).
RUN npm install --no-audit --no-fund \
        --fetch-retries=5 --fetch-retry-mintimeout=20000 --fetch-retry-maxtimeout=120000 \
    || npm install --no-audit --no-fund

COPY . .
RUN npm run build

EXPOSE 3001
CMD ["sh", "-c", "npx next start -H 0.0.0.0 -p ${PORT:-3001}"]
