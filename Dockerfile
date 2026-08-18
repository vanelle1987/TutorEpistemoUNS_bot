# Dockerfile for deploying the bot on Fly.io
# Uses Python 3.10-slim and runs Uvicorn serving the FastAPI app (main:app).
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install system deps for pypdf and google libs (if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .
RUN mkdir -p /app/bibliografia

# Expose default port
EXPOSE 8080

# Use PORT env var if provided by platform; fallback to 8080
CMD ["sh", "-c", "python worker.py & uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
