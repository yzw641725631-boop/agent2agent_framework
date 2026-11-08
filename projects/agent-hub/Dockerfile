FROM python:3.12-slim-bookworm@sha256:da54eb86849fe19be7584d8b3f291fcf0c53c196760ab3d70dc6382f681ef9c0 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock pyproject.toml alembic.ini ./
RUN pip install --no-cache-dir -r requirements.lock
COPY src ./src
COPY migrations ./migrations
RUN pip install --no-cache-dir --no-deps --no-build-isolation . \
    && useradd --uid 10001 --create-home hub

FROM base AS tests
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY tests ./tests
USER 10001
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime
USER 10001
EXPOSE 8765
CMD ["agent-hub", "serve", "--host", "0.0.0.0", "--port", "8765"]
