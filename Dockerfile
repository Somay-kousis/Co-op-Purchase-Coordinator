# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Prevent .pyc files and enable unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer cache)
COPY pyproject.toml ./
RUN pip install --no-cache-dir "fastapi>=0.110.0" "uvicorn[standard]>=0.28.0" "pydantic>=2.6.0"

# Copy source
COPY main.py ./

# Cloud Run injects PORT; default to 8080 for local Docker runs
ENV PORT=8080

EXPOSE 8080

# Use shell form so $PORT is expanded at runtime
CMD uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1
