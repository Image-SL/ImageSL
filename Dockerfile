# ImageSL backend — deployed to an AWS Lightsail container service by
# .github/workflows/deploy.yml on every push to main.
# Slim Python base + the few system libs scikit-image / imagecodecs need.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# System libraries for image decoding (JPEG, PNG, TIFF, OpenMP for skimage).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        zlib1g \
        libtiff6 \
        libopenjp2-7 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/requirements.txt ./requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY server/ ./

# The version lives in exactly one file (see desktop/BUILD.md). deploy.yml also
# passes it as IMAGESL_VERSION, which wins; carrying it here as well means a
# plain `docker run` of this image still reports what it actually is instead of
# falling back to 0.0.0-dev.
COPY version.txt ./version.txt

# Bind to $PORT where the platform injects one; default to 8000 otherwise.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
