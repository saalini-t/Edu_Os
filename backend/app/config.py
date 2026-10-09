"""Environment-driven configuration. Fails fast on unsafe or unsupported values."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_internal_url(url: str) -> bool:
    """True for loopback, private-range and single-label (docker service / host.docker.internal) hosts."""
    import ipaddress
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "host.docker.internal") or ("." not in host and host):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return host.endswith(".local") or host.endswith(".internal")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str
    jwt_secret: str = Field(min_length=32)
    jwt_ttl_minutes: int = Field(default=60, ge=1, le=24 * 60)
    storage_dir: str = "./storage"
    max_upload_bytes: int = 10 * 1024 * 1024
    max_pdf_pages: int = 200
    chunk_words: int = Field(default=120, ge=20)
    chunk_overlap_words: int = Field(default=20, ge=0)
    cors_origins: str = "http://localhost:5173"

    # Language-model provider. 'fake' (default) needs no credentials; 'ollama' is the preferred real provider.
    llm_provider: Literal["fake", "ollama", "openai_compatible"] = "fake"
    llm_base_url: str | None = None           # ollama default: http://localhost:11434 ; openai_compatible: required
    llm_model: str | None = None              # required for any non-fake provider
    llm_api_key: SecretStr | None = None      # server-side only; never logged or returned
    llm_temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    llm_timeout_s: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=1, ge=0, le=3)
    # Student text is sent to the model. Hosts other than loopback/private networks are refused unless this is set explicitly.
    llm_allow_external: bool = False

    # Policy thresholds: UNTUNED development defaults (docs/EVALUATION_PLAN.md section 6).
    t_clarify: float = 0.5
    ret_min_chunks: int = 1
    ret_min_terms: int = Field(default=4, ge=1)   # distinct query terms a passage must match to count as support
    ret_min_sim: float | None = Field(default=None, ge=-1.0, le=1.0)  # cosine similarity that also counts as support (None = off)

    # Document ingestion. 'async' = upload returns 202 QUEUED and a worker (python -m app.worker) indexes it.
    ingestion_mode: Literal["sync", "async"] = "async"
    redis_url: str | None = None              # wake-up queue only; PostgreSQL is the source of truth for jobs
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    job_lease_seconds: int = Field(default=120, ge=5)
    job_retry_backoff_seconds: float = Field(default=5.0, ge=0)   # attempt n waits backoff * 2^(n-1)
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    worker_sweep_seconds: float = Field(default=15.0, gt=0)

    # Parsing / OCR. OCR is optional and only used for pages whose text layer is (almost) empty.
    ocr_engine: Literal["none", "rapidocr", "fake"] = "none"      # 'fake' is a test stand-in
    ocr_min_text_chars: int = Field(default=20, ge=1)             # fewer non-space chars than this => try OCR
    ocr_max_pages: int = Field(default=20, ge=0)                  # OCR budget per document (extra pages are reported, not OCRed)
    ocr_dpi: int = Field(default=150, ge=72, le=300)
    ocr_max_pixels: int = Field(default=6_000_000, ge=100_000)    # render scale is reduced to stay under this
    parser_isolation: Literal["inline", "subprocess"] = "subprocess"   # parse untrusted PDFs in a child process
    parser_timeout_s: float = Field(default=120.0, gt=0)
    parser_memory_mb: int = Field(default=0, ge=0)                # POSIX only (RLIMIT_AS); 0 = no limit

    # Retrieval (Phase 2). 'hybrid' degrades to full-text-only, visibly, when embeddings are unavailable.
    retrieval_mode: Literal["fts", "hybrid"] = "hybrid"
    embedding_provider: Literal["none", "sentence_transformers", "hash"] = "none"   # 'hash' is a test-only stand-in
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_allow_download: bool = True     # downloads PUBLIC model weights once; no student data is ever sent
    embedding_batch_size: int = Field(default=16, ge=1, le=256)
    rrf_k: int = Field(default=60, ge=1)
    candidate_pool: int = Field(default=20, ge=1, le=200)
    t_master: float = 0.75           # untuned; lowered from 0.8 so that 3 clean correct answers (mean 0.8 before decay) pass
    n_min: int = 3
    max_actions: int = Field(default=6, ge=0)
    max_clarify_rounds: int = Field(default=2, ge=0)
    max_explain_attempts: int = Field(default=2, ge=0)
    max_practice_sets: int = Field(default=2, ge=0)

    # Practice and grading
    practice_set_size: int = Field(default=3, ge=1, le=6)
    check_pass_fraction: float = Field(default=1.0, gt=0, le=1.0)   # share of graded items that must be correct for a check to pass
    grader_max_uncertainty: float = Field(default=0.5, ge=0, le=1)  # model grades above this are shown but are NOT evidence

    # Mastery model (Beta-Bernoulli with recency decay over the evidence ledger). All values are untuned defaults.
    mastery_alpha0: float = Field(default=1.0, gt=0)
    mastery_beta0: float = Field(default=1.0, gt=0)
    mastery_half_life_days: float = Field(default=14.0, gt=0)
    mastery_min_distinct_sources: int = Field(default=2, ge=1)      # distinct items / teacher assessments needed for "demonstrated"
    w_attempt_exact: float = Field(default=1.0, gt=0, le=1.0)       # objectively graded attempt
    w_attempt_llm: float = Field(default=0.5, gt=0, le=1.0)         # model-graded attempt counts for less
    w_teacher: float = Field(default=2.0, gt=0, le=3.0)             # teacher assessment: larger than one attempt, still bounded
    difficulty_factor_easy: float = Field(default=0.75, gt=0, le=1.0)
    hypothesis_confirm_failures: int = Field(default=2, ge=2)       # failed attempts on DISTINCT targeted items to confirm a gap
    hypothesis_refute_successes: int = Field(default=2, ge=2)
    hypothesis_expiry_days: int = Field(default=30, ge=1)
    error_tag_window_days: int = Field(default=14, ge=1)

    # Teacher matching and escalation
    escalation_ttl_hours: int = Field(default=48, ge=1)             # unanswered escalations expire; the run ends UNRESOLVED
    match_window_hours: int = Field(default=72, ge=1)
    teacher_max_open: int = Field(default=5, ge=1)
    match_w_topic: float = 0.40
    match_w_availability: float = 0.25
    match_w_language: float = 0.15
    match_w_feedback: float = 0.10
    match_w_load: float = 0.10

    seed_demo_password: str = "eduos-demo-2026"

    @field_validator("ret_min_sim", "llm_base_url", "llm_model", "llm_api_key", mode="before")
    @classmethod
    def _empty_means_unset(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("jwt_secret")
    @classmethod
    def _no_placeholder_secret(cls, v: str) -> str:
        if v.lower().startswith(("change", "secret", "your")):
            raise ValueError("JWT_SECRET looks like a placeholder")
        return v

    @model_validator(mode="after")
    def _checks(self) -> "Settings":
        if self.llm_provider != "fake":
            if not self.llm_model:
                raise ValueError("LLM_MODEL is required when LLM_PROVIDER is not 'fake'")
            if self.llm_provider == "openai_compatible" and not self.llm_base_url:
                raise ValueError("LLM_BASE_URL is required for LLM_PROVIDER=openai_compatible")
            if not self.llm_allow_external and not _is_internal_url(self.llm_base_url or "http://localhost:11434"):
                raise ValueError("LLM_BASE_URL points outside loopback/private networks: student text would leave this "
                                 "environment. Set LLM_ALLOW_EXTERNAL=true only if that is intended and permitted.")
        if self.embedding_provider == "hash" and self.app_env == "production":
            raise ValueError("EMBEDDING_PROVIDER=hash is a test stand-in and cannot be used in production")
        if self.chunk_overlap_words >= self.chunk_words:
            raise ValueError("CHUNK_OVERLAP_WORDS must be smaller than CHUNK_WORDS")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
