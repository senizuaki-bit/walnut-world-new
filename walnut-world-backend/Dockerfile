FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates docker.io git \
    && rm -rf /var/lib/apt/lists/*

# The build context is the directory containing the backend and sibling Agent
# workspaces, so the current contract package is installed alongside the backend.
COPY agent /tmp/agent
COPY walnut-world-backend /app

RUN python /app/scripts/verify_contract_release.py \
        --agent-repo /tmp/agent \
        --release /app/contract-release.json \
    && pip install --no-cache-dir /tmp/agent /app

EXPOSE 8000

CMD ["uvicorn", "walnut_backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
