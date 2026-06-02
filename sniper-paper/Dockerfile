FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libgomp1 git tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Kolkata

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
# Install CPU-only torch first from the dedicated index so the dependency
# resolver for `pip install -e .` finds it already satisfied and does NOT
# pull the multi-GB CUDA build from PyPI.
RUN pip install --upgrade pip \
    && pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.2" \
    && pip install -e .

COPY configs ./configs
COPY sql ./sql
COPY frontend ./frontend
RUN mkdir -p ./artifacts ./data

EXPOSE 8001

# Runner is the default. Compose overrides this for the api/training services.
CMD ["sniper-paper", "run"]
