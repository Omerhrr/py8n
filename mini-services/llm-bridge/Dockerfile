# Py8n LLM bridge image (Bun + OpenAI-compatible shim)
FROM oven/bun:1

WORKDIR /app

COPY package.json ./
COPY index.ts ./

EXPOSE 3010
CMD ["bun", "run", "index.ts"]
