# Postgres with the two search extensions OctoForge's retrieval uses:
#
#   vector         (pgvector, already in the base image) — nearest-neighbour
#                  search over embeddings, so recall does not pull the whole
#                  table into the process
#   pg_textsearch  (TigerData, PostgreSQL-licensed) — real BM25 ranking, which
#                  is what finds the exact term an embedding glosses over:
#                  product names, error codes, field names, rare acronyms
#
# pg_textsearch delegates tokenization to Postgres's own `to_tsvector`, so it
# inherits the built-in language configurations (including `russian`) rather
# than bringing a tokenizer of its own. That is why this is a thin layer on the
# stock pgvector image and not a third-party Postgres distribution: everything
# except the BM25 index type is ordinary Postgres.
#
# Both extensions are OPTIONAL to OctoForge. A deployment on managed Postgres
# (RDS, Cloud SQL, Supabase, Neon) cannot install pg_textsearch — it needs
# shared_preload_libraries — and will get pgvector alone, or neither. The app
# probes for them at startup, reports what it found, and degrades to in-process
# brute-force ranking. Nothing here is required for OctoForge to run.
FROM pgvector/pgvector:pg18

# Pinned with a checksum: this lands in the database's startup path via
# shared_preload_libraries, so it is not something to float on a tag.
ARG PG_TEXTSEARCH_VERSION=1.3.1
ARG PG_TEXTSEARCH_SHA256_AMD64=9f256dc7f082235d8fead7df361427e0a0344a9ee64662f3b50449eeb90bbc7c
ARG PG_TEXTSEARCH_SHA256_ARM64=62b6351a320c82dcf802d15704b6c282f2eb03df29ed9906d5c56fafc6c21b83

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64) checksum="${PG_TEXTSEARCH_SHA256_AMD64}" ;; \
        arm64) checksum="${PG_TEXTSEARCH_SHA256_ARM64}" ;; \
        *) echo "pg_textsearch publishes no build for ${arch}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl unzip; \
    curl -fsSL -o /tmp/pg_textsearch.zip \
        "https://github.com/timescale/pg_textsearch/releases/download/v${PG_TEXTSEARCH_VERSION}/pg-textsearch-v${PG_TEXTSEARCH_VERSION}-pg18-${arch}.zip"; \
    echo "${checksum}  /tmp/pg_textsearch.zip" | sha256sum -c -; \
    unzip -j /tmp/pg_textsearch.zip -d /tmp; \
    dpkg -i "/tmp/pg-textsearch-postgresql-18_${PG_TEXTSEARCH_VERSION}-1_${arch}.deb"; \
    rm -rf /tmp/pg_textsearch.zip /tmp/*.deb; \
    apt-get purge -y --auto-remove curl unzip; \
    rm -rf /var/lib/apt/lists/*

# The extension uses shared memory and hooks, so it has to be preloaded — see
# the `command:` of the postgres service in docker-compose.yml, which keeps the
# setting next to the image that provides the library. Splitting them is how a
# database ends up refusing to start.
