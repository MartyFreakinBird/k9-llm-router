FROM node:20-alpine

WORKDIR /app

# Install deps
COPY package*.json ./
RUN npm ci --omit=dev

# Copy source
COPY . .

# Type compile
RUN npx tsc --noEmit || true

EXPOSE 8780

CMD ["npx", "ts-node", "lovable-bridge/server.ts"]
