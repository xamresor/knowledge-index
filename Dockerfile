# syntax=docker/dockerfile:1
# Knowledge Index for a shared server: kb-http with graphify (and rustworkx) and qmd in one image.
# Build:  docker build -t knowledge-index:local .
# Run:    docker/compose.example.yml, docs/docker.md.
# A local single-user install does not need this image: `uv tool install knowledge-index`.
#
# Every version is pinned. Change a version here and rebuild; tests/test_docker.py checks the pins.

# --- Node stage: qmd (docs search) and the run_code sandbox packages ---------------------------------
FROM node:24.22.2-bookworm-slim AS node
ARG QMD_VERSION=2.5.3
# The llama.cpp builds for CUDA and Vulkan are about 670 MB, and a server CPU does not use them.
# KEEP_GPU=1 keeps them; then also run the container with QMD_FORCE_CPU=0.
ARG KEEP_GPU=0
RUN npm install -g --no-audit --no-fund "@tobilu/qmd@${QMD_VERSION}" \
 && npm cache clean --force \
 && if [ "$KEEP_GPU" != 1 ]; then rm -rf /usr/local/lib/node_modules/@tobilu/qmd/node_modules/@node-llama-cpp/*-cuda* \
      /usr/local/lib/node_modules/@tobilu/qmd/node_modules/@node-llama-cpp/*-vulkan; fi \
 && node -e "require('/usr/local/lib/node_modules/@tobilu/qmd/node_modules/better-sqlite3')(':memory:').close()"  # the native binary loads
COPY sandbox/package.json sandbox/package-lock.json /sandbox/
RUN cd /sandbox && npm ci --ignore-scripts --no-audit --no-fund && npm cache clean --force

# --- Python stage: graphify with rustworkx, and the knowledge-index package, in one venv -------------
FROM python:3.13.18-slim-bookworm AS py
ARG GRAPHIFY_VERSION=0.9.25
ARG RUSTWORKX_VERSION=0.18.1
ARG HATCHLING_VERSION=1.32.4
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/kb \
 && /opt/kb/bin/pip install "graphifyy==${GRAPHIFY_VERSION}" "rustworkx==${RUSTWORKX_VERSION}"
COPY . /src
RUN /opt/kb/bin/pip install "hatchling==${HATCHLING_VERSION}" \
 && /opt/kb/bin/pip install --no-build-isolation /src \
 && /opt/kb/bin/pip uninstall -y hatchling
# The sandbox packages go next to runner.mjs inside the installed package (read-only at run time).
COPY --from=node /sandbox/node_modules /tmp/sandbox-node_modules
RUN site="$(/opt/kb/bin/python -c 'import knowledge_index, os; print(os.path.dirname(os.path.dirname(knowledge_index.__file__)))')" \
 && mv /tmp/sandbox-node_modules "$site/sandbox/node_modules" \
 && ln -s "$site/sandbox" /opt/kb/sandbox

# --- Runtime -----------------------------------------------------------------------------------------
FROM python:3.13.18-slim-bookworm
LABEL org.opencontainers.image.title="knowledge-index" \
      org.opencontainers.image.description="Shared Knowledge Index server: kb-http, graphify, qmd" \
      org.opencontainers.image.source="https://github.com/xamresor/knowledge-index" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
# rsync stages the indexed code, git lets kb-refresh read the HEADs of the read-only checkouts,
# ripgrep makes code_grep fast. EXTRA_PACKAGES adds Debian packages, e.g. the docs extractors:
#   docker build --build-arg EXTRA_PACKAGES="poppler-utils pandoc tesseract-ocr" .
ARG EXTRA_PACKAGES=""
RUN apt-get update \
 && apt-get install -y --no-install-recommends rsync git ripgrep ca-certificates libstdc++6 $EXTRA_PACKAGES \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system safe.directory '*'
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/@tobilu/qmd /usr/local/lib/node_modules/@tobilu/qmd
RUN ln -s /usr/local/lib/node_modules/@tobilu/qmd/bin/qmd /usr/local/bin/qmd
COPY --from=py /opt/kb /opt/kb
# Optional plugins, e.g. the KB's own users: kb addon install /opt/kb/contrib/users
COPY contrib /opt/kb/contrib
RUN groupadd --gid 10001 kb \
 && useradd --uid 10001 --gid kb --home-dir /home/kb --create-home --shell /usr/sbin/nologin kb \
 && mkdir -p /var/lib/knowledge-index /etc/knowledge-index /run/kb-sandbox /routes \
 && chown kb:kb /var/lib/knowledge-index /etc/knowledge-index /run/kb-sandbox

# Data: /var/lib/knowledge-index (graph, staged code, docs index, qmd models, logs, users).
# Config: /etc/knowledge-index (kb.projects.toml, kb.hubs.txt). qmd keeps its index and its
# collection list in the data folder too, so one volume holds everything a build makes.
ENV PATH=/opt/kb/bin:$PATH \
    XDG_DATA_HOME=/var/lib \
    XDG_CONFIG_HOME=/etc \
    XDG_CACHE_HOME=/var/lib/knowledge-index/cache \
    QMD_CONFIG_DIR=/var/lib/knowledge-index/qmd \
    QMD_FORCE_CPU=1 \
    KB_HTTP_HOST=0.0.0.0 \
    KB_HTTP_PORT=8790 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
VOLUME ["/var/lib/knowledge-index"]
USER kb
WORKDIR /var/lib/knowledge-index
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD ["python3", "-c", "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ.get('KB_HTTP_PORT', '8790'))), 3)"]
# kb-http refuses to start until KB_SERVER=on and KB_PERMISSION_URL are set (or an access plugin is on).
CMD ["kb-http"]
