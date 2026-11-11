# The pages (/connect, /plan/<id>): web/pages -> mcp_server/web/static/app (the Stellar MCP's page components, Tailwind, and
# the email / social wallet through Dynamic).
FROM node:24-slim AS pages
WORKDIR /build/web/pages
COPY web/pages/package.json web/pages/package-lock.json ./
RUN npm ci --no-audit --no-fund --ignore-scripts
COPY web/pages/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_TRANSPORT=streamable-http \
    MCP_HTTP_HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

COPY pyproject.toml .
COPY vanna_core ./vanna_core
COPY mcp_server ./mcp_server
COPY --from=pages /build/mcp_server/web/static/app ./mcp_server/web/static/app
COPY idl ./idl

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8080
CMD ["python", "-m", "mcp_server.main"]
