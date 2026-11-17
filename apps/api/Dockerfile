# ---- Build stage ----
FROM python:3.11-slim AS builder

WORKDIR /build

ARG PYTHON_REQUIREMENTS=requirements-full.txt

COPY requirements-core.txt requirements-full.txt requirements.txt ./
RUN pip install --no-cache-dir --retries 8 --timeout 180 --prefix=/install -r "${PYTHON_REQUIREMENTS}"

# ---- Runtime stage ----
FROM python:3.11-slim

WORKDIR /app
RUN apt-get -o Acquire::ForceIPv4=true update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Runtime-only system deps (curl for healthcheck, docker CLI for sandbox)

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Create non-root user
RUN groupadd -r opentutor && useradd -r -g opentutor -d /app -s /sbin/nologin opentutor \
    && mkdir -p /app/uploads /app/data && chown -R opentutor:opentutor /app

COPY --chown=opentutor:opentutor . .

USER opentutor

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
