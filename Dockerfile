FROM python:3.11-slim

WORKDIR /app

# libgomp1 is needed by torch (a sentence-transformers dependency) at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# CPU-only torch by default -- the plain PyPI `torch` wheel drags in a pile of
# unused nvidia-*/cuda-toolkit packages even without a GPU. Override for a
# CUDA build: docker build --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124
# (also wire up GPU passthrough in docker-compose.yml's `deploy.resources.reservations.devices`
# and run the container with the NVIDIA Container Toolkit -- not needed for CPU).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

# Dependencies live in their own layer keyed ONLY on the two pyproject.toml
# files: a source-code change must not re-download torch. The extraction reads
# the manifests directly (no package build, so no sources are needed yet); the
# local octoforge-core dependency of web is skipped -- it is installed from
# sources below.
COPY core/pyproject.toml /tmp/deps/core.toml
COPY web/pyproject.toml /tmp/deps/web.toml
RUN <<'SH'
set -e
python - > /tmp/deps/requirements.txt <<'PY'
import tomllib


def load(path, extras=()):
    with open(path, "rb") as fh:
        project = tomllib.load(fh)["project"]
    deps = list(project.get("dependencies", []))
    for extra in extras:
        deps += project.get("optional-dependencies", {}).get(extra, [])
    return deps


deps = load("/tmp/deps/core.toml", ("local-embeddings", "postgres"))
deps += [dep for dep in load("/tmp/deps/web.toml") if not dep.startswith("octoforge-core")]
print("\n".join(deps))
PY
pip install --no-cache-dir --extra-index-url ${TORCH_INDEX_URL} -r /tmp/deps/requirements.txt
SH

# Sources come last: rebuilding after a code change only re-runs this cheap
# --no-deps install on top of the cached dependency layer.
COPY core/ ./core/
COPY web/ ./web/
RUN pip install --no-cache-dir --no-deps ./core ./web

EXPOSE 8000

CMD ["uvicorn", "octoforge_web.main:app", "--host", "0.0.0.0", "--port", "8000"]
