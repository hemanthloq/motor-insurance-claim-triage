# =============================================================================
#  One image, four roles.
#
#  The api, worker, llm-stub and tools services all run from this image with a
#  different command. They share almost all of their code -- the contracts, the
#  index client, the telemetry, the config -- so building four images would mean
#  four copies of the same layers, four pulls and four chances for them to drift
#  apart on a dependency bump.
#
#  Two stages, because the split that *does* pay for itself is the dependency
#  one. `runtime` carries the lightweight set; `runtime-ml` adds
#  sentence-transformers and chromadb, which pull in torch and are needed only
#  by the processes that embed text. The api image is roughly a tenth the size
#  and starts in about a second, which matters for a readiness probe.
# =============================================================================

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Run as a non-root user. Nothing in this service needs root, and a container
# that cannot write outside its data volume is one less thing to reason about.
RUN useradd --create-home --uid 10001 claimtriage \
 && mkdir -p /data/index /data/hf \
 && chown -R claimtriage:claimtriage /app /data


# -----------------------------------------------------------------------------
#  runtime -- api, llm-stub
# -----------------------------------------------------------------------------
FROM base AS runtime

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=claimtriage:claimtriage claimtriage ./claimtriage
COPY --chown=claimtriage:claimtriage prompts ./prompts
COPY --chown=claimtriage:claimtriage corpus/sources.yaml ./corpus/sources.yaml
COPY --chown=claimtriage:claimtriage eval ./eval
COPY --chown=claimtriage:claimtriage web ./web

USER claimtriage
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "claimtriage.api.app:app", "--host", "0.0.0.0", "--port", "8080"]


# -----------------------------------------------------------------------------
#  runtime-ml -- worker, tools (indexing and evaluation)
# -----------------------------------------------------------------------------
FROM base AS runtime-ml

ENV HF_HOME=/data/hf \
    SENTENCE_TRANSFORMERS_HOME=/data/hf \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=1

COPY requirements.txt requirements-heavy.txt ./
# CPU-only torch. The GPU wheels are ~2 GB and this model does not need them.
RUN pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      -r requirements-heavy.txt

COPY --chown=claimtriage:claimtriage claimtriage ./claimtriage
COPY --chown=claimtriage:claimtriage prompts ./prompts
COPY --chown=claimtriage:claimtriage corpus ./corpus
COPY --chown=claimtriage:claimtriage eval ./eval

USER claimtriage
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "claimtriage.worker.executor"]
