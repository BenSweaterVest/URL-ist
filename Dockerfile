# URLer — Container Image
# =======================================
# Single-container app: FastAPI backend + React SPA served as static files.
# No separate frontend build step — the SPA uses React via CDN and Babel
# standalone for JSX transpilation directly in the browser.
#
# /data is the persistent config directory, mounted as a Docker volume.
# The setup wizard writes /data/config.json on first launch.

FROM python:3.12-slim

# Don't write .pyc files and ensure stdout/stderr are unbuffered
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Create a non-root user to run the application.
# Running as root is unnecessary and increases attack surface.
RUN groupadd --system --gid 1000 appgroup \
 && useradd  --system --uid 1000 --gid appgroup --no-create-home appuser

# Create the config directory with the app user as owner.
# Docker named volumes copy the image's /data on first creation, so the
# ownership set here is preserved and appuser can write config.json.
RUN mkdir -p /data \
 && chown appuser:appgroup /data \
 && chmod 750 /data

# Install Python dependencies before copying application code so this layer
# is cached and only rebuilt when requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY main.py .
COPY static/ ./static/

EXPOSE 8000

# Drop to non-root user for the running process
USER appuser

# Single worker — this is a low-traffic internal tool.
# log-level=info surfaces startup config and per-request logs.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info", "--workers", "1"]
