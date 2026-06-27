FROM python:3.12-slim

# Install system dependencies (git is required for orchestrator git tasks, curl for reachability checks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Run the python supervisor
ENTRYPOINT ["python", "scripts/entrypoint.py"]
