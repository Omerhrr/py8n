# Py8n frontend image (Nuxt 3)
FROM oven/bun:1 AS build

WORKDIR /app
COPY package.json ./
RUN bun install
COPY . .
RUN bun run build

FROM oven/bun:1
WORKDIR /app
COPY --from=build /app/.output ./.output

ENV NODE_ENV=production
ENV PORT=3000
EXPOSE 3000
CMD ["bun", ".output/server/index.mjs"]
