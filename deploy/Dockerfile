FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY mcp/budget.py mcp/server.py ./
USER 10001:10001
EXPOSE 8000
CMD ["python", "server.py", "--transport", "http", "--bind", "0.0.0.0", "--port", "8000"]
