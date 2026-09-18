# Slim Python image; matches requires-python >= 3.12
FROM python:3.12-slim

# No .pyc files, unbuffered logs for live container output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Install the package (deps + app code)
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# Non-root user for basic hardening
RUN useradd --create-home appuser
USER appuser

# Same image for local dev, Render/Railway deploy, and the Docker fallback reference
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
