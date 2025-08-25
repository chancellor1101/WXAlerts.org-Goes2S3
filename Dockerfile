FROM python:3.12-slim

# System deps (optional: ca-certificates for TLS)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# App
COPY app.py /app/app.py

# Non-privileged user
RUN useradd -ms /bin/bash appuser
USER appuser

WORKDIR /app

# Healthcheck: simple ping that exits 0
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 CMD python -c "import os; print('ok')"

# Default command
CMD ["python", "/app/app.py"]
