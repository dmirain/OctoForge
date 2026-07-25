FROM python:3.11-slim

WORKDIR /app

# libgomp1 is needed by torch (a sentence-transformers dependency) at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY core/ ./core/
COPY web/ ./web/

# CPU-only torch by default -- the plain PyPI `torch` wheel drags in a pile of
# unused nvidia-*/cuda-toolkit packages even without a GPU. Override for a
# CUDA build: docker build --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124
# (also wire up GPU passthrough in docker-compose.yml's `deploy.resources.reservations.devices`
# and run the container with the NVIDIA Container Toolkit -- not needed for CPU).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir --extra-index-url ${TORCH_INDEX_URL} "./core[local-embeddings,postgres]" \
    && pip install --no-cache-dir ./web

EXPOSE 8000

CMD ["uvicorn", "octoforge_web.main:app", "--host", "0.0.0.0", "--port", "8000"]
