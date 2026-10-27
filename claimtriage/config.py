"""Runtime configuration.

Everything the system needs to talk to the outside world arrives here, from the
environment, at process start (D4). Nothing in this file carries a default that
points at a real endpoint, a real model or a credential -- the blanks are blank
on purpose, and `.env.example` documents them.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---- identity -----------------------------------------------------------
    service_name: str = "claim-triage"
    service_role: str = "api"  # api | worker | stub | cli
    environment: str = "local"

    # ---- model provider -----------------------------------------------------
    llm_provider: Literal["openai_compat", "gemini", "stub"] = "stub"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.0
    llm_max_output_tokens: int = 1200
    llm_price_prompt_per_mtok: float = 0.0
    llm_price_completion_per_mtok: float = 0.0

    # ---- resilience (A5) ----------------------------------------------------
    llm_timeout_seconds: float = 30.0
    llm_max_attempts: int = 3
    llm_backoff_base_seconds: float = 0.5
    llm_backoff_max_seconds: float = 8.0
    circuit_fail_threshold: int = 5
    circuit_reset_seconds: float = 30.0
    tool_timeout_seconds: float = 10.0

    # ---- agent loop bounds (A1) ---------------------------------------------
    agent_max_steps: int = 14
    agent_max_tool_calls: int = 12
    agent_wall_clock_seconds: float = 120.0
    retrieval_top_k: int = 6
    verifier_min_support: float = 0.60
    escalation_min_retrieval_score: float = 0.25

    # ---- retrieval (L1) -----------------------------------------------------
    embedding_backend: Literal["sentence_transformers", "hash"] = "hash"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dim: int = 384
    chunk_tokens: int = 320
    chunk_overlap_tokens: int = 64
    vector_backend: Literal["chroma_http", "local"] = "local"
    chroma_host: str = "chroma"
    chroma_port: int = 8000
    chroma_collection: str = "policy_clauses"
    index_dir: str = "./data/index"

    # ---- infrastructure -----------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "triage:queue"
    execution_mode: Literal["queue", "inline"] = "queue"
    worker_concurrency: int = 2
    run_ttl_seconds: int = 86_400

    # ---- caching (D7) -------------------------------------------------------
    cache_embeddings: bool = True
    cache_retrievals: bool = True
    cache_runs: bool = True
    cache_ttl_seconds: int = 3600

    # ---- rate limiting (D7) -------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 30
    rate_limit_window_seconds: int = 60

    # ---- guardrails (L4) ----------------------------------------------------
    guardrails_enabled: bool = True
    toxicity_threshold: float = 0.60
    injection_threshold: float = 0.50

    # ---- prompts (L2) -------------------------------------------------------
    prompt_set: str = "v2"
    prompt_dir: str = "./prompts"

    # ---- observability ------------------------------------------------------
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "claim-triage"
    tracing_enabled: bool = False
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # ---- SLO (D6) -----------------------------------------------------------
    slo_p95_seconds: float = 45.0

    # ---- stub provider ------------------------------------------------------
    stub_base_url: str = "http://llm-stub:9009"
    stub_latency_ms: int = 350
    stub_latency_jitter_ms: int = 150
    stub_failure_rate: float = 0.0

    corpus_manifest: str = Field(default="./corpus/sources.yaml")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
