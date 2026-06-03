FROM node:20-alpine

WORKDIR /app

ARG NEXT_PUBLIC_API_URL=""
ARG NEXT_PUBLIC_WS_URL=""

ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_WS_URL=$NEXT_PUBLIC_WS_URL

COPY package*.json ./
# The deploy builds this image --no-cache (to dodge a stale COPY-layer bug),
# so npm install runs fresh every deploy and is exposed to npm-registry blips.
# fetch-retries + --no-audit/--no-fund make it resilient (a transient install
# failure red-failed an otherwise-clean deploy on 2026-06-03). Falls back to a
# plain retry if the first attempt still trips.
RUN npm install --no-audit --no-fund \
        --fetch-retries=5 --fetch-retry-mintimeout=20000 --fetch-retry-maxtimeout=120000 \
    || npm install --no-audit --no-fund

COPY . .
RUN npm run build

EXPOSE 8080
CMD ["sh", "-c", "npx next start -H 0.0.0.0 -p ${PORT:-3000}"]
