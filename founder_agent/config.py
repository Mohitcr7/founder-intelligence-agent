"""Runtime configuration. Everything is env-overridable; nothing is hardcoded
to a single model vendor."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _var_dir() -> Path:
    return Path(os.environ.get("FA_VAR_DIR", ROOT / "var"))


#: Trust registry. Authority is a property of the channel a document arrived
#: through, never of the `authority:` line written inside it - a forged board
#: minute filed as an upload stays an upload. Ops-owned: adding Outlook, Teams
#: or Slack is one row here plus a connector that pushes into /api/ingest/<row>.
#:   can_govern  - may a document from this channel become governing policy?
#:   quarantine  - is its text withheld from answers until a human clears it?
#:   sensitivity - default release class for its claims.
TRUST_REGISTRY: dict[str, dict] = {
    "board":       {"authority": "board",       "can_govern": True,  "quarantine": False, "sensitivity": "internal"},
    "founder":     {"authority": "founder",     "can_govern": True,  "quarantine": False, "sensitivity": "internal"},
    "compliance":  {"authority": "compliance",  "can_govern": True,  "quarantine": False, "sensitivity": "internal"},
    "finance":     {"authority": "finance",     "can_govern": True,  "quarantine": False, "sensitivity": "internal"},
    # Low rank: may contest a board record, never silently replace it.
    "engineering": {"authority": "engineering", "can_govern": True,  "quarantine": False, "sensitivity": "internal"},
    "external":    {"authority": "external",    "can_govern": False, "quarantine": False, "sensitivity": "public"},
    "upload":      {"authority": "unknown",     "can_govern": False, "quarantine": True,  "sensitivity": "internal"},
}

#: Placeholder credentials accepted in development only. `Settings.validate`
#: refuses to start a production process that still carries any of them.
DEV_SECRET_PREFIX = "dev-"


@dataclass
class Settings:
    environment: str = os.environ.get("FA_ENV", "development")
    corpus_dir: Path = ROOT / "corpus"
    var_dir: Path = field(default_factory=_var_dir)
    audit_path: Path = field(default_factory=lambda: _var_dir() / "audit.jsonl")
    actions_path: Path = field(default_factory=lambda: _var_dir() / "actions.jsonl")
    #: Documents received through /api/ingest land here, with their provenance.
    intake_dir: Path = field(default_factory=lambda: _var_dir() / "intake")
    #: Chain heads are copied here. Point it at storage this process cannot
    #: overwrite (WORM bucket, append-only mount) so a full rewrite of the log
    #: no longer verifies clean.
    audit_anchor_path: Path = field(default_factory=lambda: Path(
        os.environ.get("FA_AUDIT_ANCHOR", _var_dir() / "anchor" / "audit_heads.jsonl")))
    audit_anchor_every: int = _env_int("FA_AUDIT_ANCHOR_EVERY", 25)

    # Retrieval
    top_k: int = _env_int("FA_TOP_K", 8)
    #: Below this best-score the system answers UNKNOWN rather than guessing.
    min_evidence_score: float = _env_float("FA_MIN_EVIDENCE", 1.2)
    #: Below this share of question terms known to the corpus (with at least
    #: two unknown terms), the question is about something the corpus lacks.
    min_coverage: float = _env_float("FA_MIN_COVERAGE", 0.5)
    #: hashing (offline default) | http (OpenAI-compatible endpoint) | none
    embeddings: str = os.environ.get("FA_EMBEDDINGS", "hashing")
    embeddings_url: str = os.environ.get("FA_EMBEDDINGS_URL", "https://api.openai.com/v1")
    embeddings_model: str = os.environ.get("FA_EMBEDDINGS_MODEL", "text-embedding-3-small")
    embeddings_key_env: str = os.environ.get("FA_EMBEDDINGS_KEY_ENV", "OPENAI_API_KEY")

    # Routing: task role -> provider preference order.
    role_routing: dict[str, list[str]] = field(default_factory=lambda: {
        "reason": _split(os.environ.get("FA_ROUTE_REASON", "anthropic,openai,deterministic")),
        "extract": _split(os.environ.get("FA_ROUTE_EXTRACT", "anthropic,openai,deterministic")),
        "research": _split(os.environ.get("FA_ROUTE_RESEARCH", "anthropic,openai,deterministic")),
        "redteam": _split(os.environ.get("FA_ROUTE_REDTEAM", "anthropic,openai,deterministic")),
    })

    anthropic_model: str = os.environ.get("FA_ANTHROPIC_MODEL", "claude-haiku-4-5")
    openai_model: str = os.environ.get("FA_OPENAI_MODEL", "gpt-4.1")
    #: Optional OpenAI-compatible local server (Ollama, llama.cpp, vLLM) for
    #: air-gapped use. Registered as provider "local" when the URL is set.
    local_url: str = os.environ.get("FA_LOCAL_URL", "")
    local_model: str = os.environ.get("FA_LOCAL_MODEL", "llama3.1")

    # Human authorization
    action_ttl_seconds: int = _env_int("FA_ACTION_TTL", 900)
    #: Shared secret that the *human* console must present to approve, deny or
    #: execute. An agent process never receives this value; see
    #: docs/THREAT_MODEL.md. Replace with SSO-backed identity in production.
    approver_token: str = os.environ.get("FA_APPROVER_TOKEN", "dev-human-console-token")
    #: Outer lock on every /api/* route, reads included.
    api_token: str = os.environ.get("FA_API_TOKEN", "dev-api-token")
    #: One credential per intake channel; `upload` needs none (it is quarantined).
    #: In production an unset token disables that channel.
    connector_tokens: dict[str, str] = field(default_factory=lambda: {
        ch: os.environ.get(f"FA_CONNECTOR_TOKEN_{ch.upper()}",
                           "" if os.environ.get("FA_ENV") == "production" else f"dev-{ch}-connector-token")
        for ch in TRUST_REGISTRY if ch != "upload"
    })

    # Guards
    max_delegation_depth: int = _env_int("FA_MAX_DEPTH", 2)
    subagent_token_budget: int = _env_int("FA_SUBAGENT_BUDGET", 4000)

    def ensure_dirs(self) -> None:
        for p in (self.audit_path, self.actions_path, self.audit_anchor_path):
            p.parent.mkdir(parents=True, exist_ok=True)
        self.intake_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        """Production refuses to start on a missing or published placeholder secret."""
        if self.environment != "production":
            return
        required = {"FA_APPROVER_TOKEN": self.approver_token, "FA_API_TOKEN": self.api_token}
        weak = [k for k, v in required.items() if not v or v.startswith(DEV_SECRET_PREFIX)]
        weak += [f"FA_CONNECTOR_TOKEN_{k.upper()}" for k, v in self.connector_tokens.items()
                 if v.startswith(DEV_SECRET_PREFIX)]
        if weak:
            raise RuntimeError(f"production refuses development credentials: set {', '.join(weak)}")


def _split(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


settings = Settings()
