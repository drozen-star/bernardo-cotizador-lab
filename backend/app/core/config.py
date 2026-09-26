"""Application settings.

Every secret and every environment-dependent knob is read here, from the
environment or from a local ``.env``. Nothing else in the codebase reads
``os.environ`` directly.

Two rules this module enforces:

* **No SMTP.** Render's free tier blocks outbound ports 25/465/587, so email is
  configured as an HTTPS provider only (see ``EMAIL_PROVIDER``).
* **No hard-coded provider.** The LLM is always addressed through an
  OpenAI-compatible ``LLM_BASE_URL`` so Groq, OpenRouter, and Gemini are a
  three-variable swap.
"""

import json
from functools import lru_cache
from typing import Annotated
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEV_SECRET_KEY = "dev-insecure-secret-change-me"
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:5432/supplier_quote_db"
)

# Provider presets, used by /health and the README so a misconfigured deployment
# is diagnosable from the API rather than only from logs.
#
# Model ids in this table have a shelf life. Providers retire models on a schedule
# and a retired id fails every request with a 404 "model_not_found", which looks
# like a broken API key rather than a stale default. Groq's own deprecation page
# is the authority:
#
#   https://console.groq.com/docs/deprecations
#
# Groq shut down `llama-3.3-70b-versatile` on 2026-08-16 and recommends
# `openai/gpt-oss-120b`. That replacement supports JSON Object Mode, which this
# codebase relies on for structured extraction and follow-up drafting, so it is a
# drop-in. If a call ever fails with `model_not_found`, this table is the first
# place to look — and `dev.cmd llm` will tell you in one command.
LLM_PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "signup": "https://console.groq.com",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        # Any model id ending in `:free`. Check the current list at
        # https://openrouter.ai/models?max_price=0 — the free roster changes.
        "model": "deepseek/deepseek-r1:free",
        "signup": "https://openrouter.ai",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        # Check https://ai.google.dev/gemini-api/docs/models for the current roster.
        "model": "gemini-2.0-flash",
        "signup": "https://aistudio.google.com",
    },
}


class Settings(BaseSettings):
    APP_NAME: str = "Supplier Quote Autopilot"
    APP_VERSION: str = "1.0.0"

    # "development" enables console email logging and verbose CORS defaults.
    ENV: str = "development"

    # ------------------------------------------------------------------ database
    # Neon **pooled** connection string for the app ("-pooler" in the hostname).
    DATABASE_URL: str = DEFAULT_DATABASE_URL
    # Neon **direct** (non-pooled) connection string, used only by Alembic. Falls
    # back to DATABASE_URL when unset so migrations also work in local dev.
    DATABASE_URL_DIRECT: str = ""

    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_RECYCLE_SECONDS: int = 300

    # ---------------------------------------------------------------------- auth
    SECRET_KEY: str = DEV_SECRET_KEY
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7

    # ---------------------------------------------------------------------- URLs
    BACKEND_URL: str = "http://localhost:8000"
    FRONTEND_URL: str = "http://localhost:5173"
    # Where the supplier-facing quote form is hosted (separate Vercel project).
    PUBLIC_FORM_URL: str = "http://localhost:5174"

    # NoDecode disables pydantic-settings' default JSON decoding for this
    # complex field, so the comma-separated .env value reaches the validator
    # below as a raw string instead of failing a json.loads() parse.
    ALLOWED_ORIGINS: Annotated[list[str], NoDecode] = ["*"]

    # ----------------------------------------------------------------------- LLM
    # Any OpenAI-compatible endpoint. Defaults to Groq's free tier.
    LLM_BASE_URL: str = LLM_PROVIDER_PRESETS["groq"]["base_url"]
    LLM_API_KEY: str = ""
    LLM_MODEL: str = LLM_PROVIDER_PRESETS["groq"]["model"]

    # Free-tier guardrails. Groq's free tier is ~30 requests/minute and
    # 14,400 requests/day per model; the client spaces requests out and retries
    # with exponential backoff on 429/5xx.
    LLM_REQUESTS_PER_MINUTE: int = 30
    LLM_MAX_CONCURRENCY: int = 2
    LLM_MAX_RETRIES: int = 4
    LLM_TIMEOUT_SECONDS: float = 60.0
    LLM_ENABLED: bool = True

    #: How much thinking a reasoning model is allowed to do before it answers:
    #: "low", "medium", "high", or "" to send nothing at all.
    #:
    #: "low" is the default because every LLM call in this application wants a small,
    #: strictly-shaped JSON answer, not an essay. On the recommended free Groq model
    #: (``openai/gpt-oss-120b``) the provider default trace was long enough to consume
    #: the whole completion budget, and Groq then rejected the request with HTTP 400
    #: ``json_validate_failed`` — "max completion tokens reached before generating a
    #: valid document" — so parsing, follow-up drafting and comparison summaries all
    #: fell back to their deterministic paths without ever saying why. Set "" when
    #: pointing LLM_MODEL at a non-reasoning model whose endpoint rejects the field.
    LLM_REASONING_EFFORT: str = "low"

    #: Hard cap on how many LLM calls one batch job may issue before stopping.
    LLM_BATCH_MAX_CALLS: int = 50

    # Legacy: the base repo's chat agents used OPENAI_API_KEY. Kept so an
    # existing .env keeps working; LLM_API_KEY wins when both are set.
    OPENAI_API_KEY: str = ""

    # --------------------------------------------------------------------- email
    # console | resend | sendgrid | mailgun | brevo   (never SMTP)
    EMAIL_PROVIDER: str = "console"
    EMAIL_API_KEY: str = ""
    # Optional override for self-hosted / regional endpoints.
    EMAIL_API_URL: str = ""
    MAIL_FROM: str = "Supplier Quote Autopilot <onboarding@resend.dev>"
    # Legacy Resend-specific key from the base repo; used when EMAIL_API_KEY is blank.
    RESEND_API_KEY: str = ""

    # ------------------------------------------------------------------- storage
    # local | s3   (s3 = any S3-compatible service: R2, B2, Neon Object Storage)
    STORAGE_BACKEND: str = "local"
    UPLOAD_DIR: str = "./var/uploads"
    MAX_UPLOAD_MB: int = 10
    ALLOWED_UPLOAD_EXTENSIONS: Annotated[list[str], NoDecode] = [
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".csv",
        ".xlsx",
        ".doc",
        ".docx",
    ]
    S3_ENDPOINT_URL: str = ""
    S3_BUCKET: str = ""
    S3_REGION: str = "auto"
    S3_ACCESS_KEY_ID: str = ""
    S3_SECRET_ACCESS_KEY: str = ""
    # Set when the bucket is fronted by a CDN / public domain; otherwise
    # downloads are proxied through the backend.
    S3_PUBLIC_BASE_URL: str = ""

    # ------------------------------------------------- public form & anti-spam
    INVITATION_TTL_DAYS: int = 30
    # Per-IP request budget for the public endpoints.
    PUBLIC_RATE_LIMIT_PER_MINUTE: int = 20
    PUBLIC_RATE_LIMIT_PER_HOUR: int = 60
    # none | turnstile | hcaptcha
    CAPTCHA_PROVIDER: str = "none"
    CAPTCHA_SITE_KEY: str = ""
    CAPTCHA_SECRET_KEY: str = ""

    # ----------------------------------------------------------------- follow-ups
    # When false (the default) drafts are queued for buyer approval. Awarding is
    # *always* human-approved regardless of this flag.
    AUTO_SEND_FOLLOWUPS: bool = False
    #: Hours after the invitation was sent, per reminder.
    FOLLOWUP_INTERVALS_HOURS: Annotated[list[int], NoDecode] = [72, 168]
    #: Also remind N hours before the RFQ deadline if still unanswered.
    FOLLOWUP_BEFORE_DEADLINE_HOURS: int = 48
    MAX_FOLLOWUPS_PER_INVITATION: int = 3
    #: Remind suppliers whose submitted quote is incomplete at most N times.
    MAX_INCOMPLETE_REMINDERS: int = 2

    SCHEDULER_ENABLED: bool = True
    SCHEDULER_INTERVAL_MINUTES: int = 15
    #: Shared secret for POST /internal/scheduler/tick (external cron).
    SCHEDULER_SECRET: str = ""

    # ---------------------------------------------------------------------- demo
    #: Serve the read-only demo workspace at GET /demo/workspace.
    #:
    #: The payload is a committed snapshot produced by the demo seed through the
    #: real engine, so a visitor with no account can see what the product computes
    #: without touching the database. It is a single GET: there is nothing to write,
    #: no token is issued, and no demo endpoint accepts a body — so "view only" is a
    #: property of the route table rather than a rule the UI is trusted to follow.
    #:
    #: Turn it off on a deployment that should not carry a demo at all.
    DEMO_MODE_ENABLED: bool = True

    # ------------------------------------------------------------- lab Bernardo
    #: Número de WhatsApp de Bernardo en E.164 sin "+", p. ej. 5491122334455. Va en el
    #: link wa.me del mail de batch (features/rfq/batch_invite.py). Vacío = el mail
    #: sale sin ese link y se loguea un warning; nada más cambia.
    BERNARDO_WA_NUMBER: str = ""

    # ----------------------------------------------------------------- comparison
    #: Default currency. SGD because this product is aimed at Singapore facilities
    #: and building-services procurement; a deployment elsewhere sets its own.
    BASE_CURRENCY: str = "SGD"

    #: Default procurement type for a new RFQ: "service" or "goods".
    #: "service" because maintenance and minor works are what this is for. It
    #: selects the required-field contract, the default scoring weights, and the
    #: vocabulary the UI and the follow-up emails use.
    DEFAULT_PROCUREMENT_TYPE: str = "service"

    #: GST (or equivalent consumption tax) applied when a supplier states a rate
    #: but no explicit tax amount. 9% is the current Singapore rate. Set to 0 for a
    #: buyer who is not GST-registered, or for a country with no such tax.
    DEFAULT_GST_RATE: float = 9.0

    #: JSON object overriding the built-in FX table, e.g. {"EUR": 0.92}.
    FX_RATES_JSON: str = ""
    #: JSON object overriding the default scoring weights.
    DEFAULT_SCORING_WEIGHTS_JSON: str = ""
    LEAD_TIME_RISK_DAYS: int = 90
    #: Quotes older than this many days are flagged as stale.
    QUOTE_VALIDITY_WARNING_DAYS: int = 7

    # ------------------------------------------------------------------ validators
    @field_validator("ALLOWED_ORIGINS", mode="before")
    @classmethod
    def parse_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @field_validator("ALLOWED_UPLOAD_EXTENSIONS", mode="before")
    @classmethod
    def parse_extensions(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            items = [item.strip().lower() for item in v.split(",") if item.strip()]
        else:
            items = [str(item).strip().lower() for item in v]
        return [item if item.startswith(".") else f".{item}" for item in items]

    @field_validator("FOLLOWUP_INTERVALS_HOURS", mode="before")
    @classmethod
    def parse_intervals(cls, v: str | list[int]) -> list[int]:
        if isinstance(v, str):
            items = [item.strip() for item in v.split(",") if item.strip()]
        else:
            items = list(v)
        return sorted({int(item) for item in items})

    @field_validator("FX_RATES_JSON", "DEFAULT_SCORING_WEIGHTS_JSON", mode="before")
    @classmethod
    def validate_json_object(cls, v: Any) -> Any:
        if v in (None, ""):
            return ""
        if isinstance(v, dict):
            return json.dumps(v)
        json.loads(v)  # raises a clear error at startup instead of at call time
        return v

    @field_validator("DATABASE_URL", "DATABASE_URL_DIRECT", mode="after")
    @classmethod
    def force_the_psycopg3_driver(cls, v: str) -> str:
        """Rewrite a provider's connection string onto the driver this app installs.

        Every hosted Postgres hands out a bare ``postgresql://`` (or the older
        ``postgres://``) URL, and SQLAlchemy reads that as "use psycopg2" — which is
        not in requirements.txt, because this codebase uses psycopg **3**. The result
        of pasting Neon's string verbatim was a crash at import:

            ModuleNotFoundError: No module named 'psycopg2'

        which looks like a broken dependency rather than a string that needs six
        characters changed. Pasting a connection string is the first thing anyone
        deploying this does, so the app does the rewrite itself. A URL that already
        names a driver is left exactly as it is, so an explicit choice still wins.
        """

        if not v:
            return v

        for scheme in ("postgresql://", "postgres://"):
            if v.startswith(scheme):
                return "postgresql+psycopg://" + v[len(scheme) :]

        return v

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    # ------------------------------------------------------------------ helpers
    @property
    def is_production(self) -> bool:
        return self.ENV.lower() in {"production", "prod"}

    @property
    def migration_database_url(self) -> str:
        """Direct (non-pooled) URL for Alembic; pooled URL is for the app."""
        return self.DATABASE_URL_DIRECT or self.DATABASE_URL

    @property
    def resolved_llm_api_key(self) -> str:
        return self.LLM_API_KEY or self.OPENAI_API_KEY

    @property
    def resolved_email_api_key(self) -> str:
        return self.EMAIL_API_KEY or self.RESEND_API_KEY

    @property
    def fx_rates(self) -> dict[str, float]:
        if not self.FX_RATES_JSON:
            return {}
        return {k.upper(): float(v) for k, v in json.loads(self.FX_RATES_JSON).items()}

    @property
    def scoring_weights(self) -> dict[str, float]:
        if not self.DEFAULT_SCORING_WEIGHTS_JSON:
            return {}
        return {
            k: float(v)
            for k, v in json.loads(self.DEFAULT_SCORING_WEIGHTS_JSON).items()
        }

    def llm_configured(self) -> bool:
        return bool(self.LLM_ENABLED and self.resolved_llm_api_key)

    def email_configured(self) -> bool:
        if self.EMAIL_PROVIDER == "console":
            return True
        return bool(self.resolved_email_api_key)

    def public_form_link(self, rfq_id: int, token: str) -> str:
        base = self.PUBLIC_FORM_URL.rstrip("/")
        return f"{base}/quote/{rfq_id}/{token}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
