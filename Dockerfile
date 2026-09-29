FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_TRANSPORT=streamable-http \
    MCP_HTTP_HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml .
COPY vanna_core ./vanna_core
COPY mcp_server ./mcp_server
COPY idl ./idl

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8080
CMD ["python", "-m", "mcp_server.main"]
