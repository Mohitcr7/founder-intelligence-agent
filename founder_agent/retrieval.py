"""Hybrid retrieval over claims: BM25 fused with an embedding signal.

BM25 stays the backbone: it is deterministic, offline, and its score is what
the UNKNOWN floor is calibrated against. An embedder adds a similarity term on
top, so a paraphrase can still find its claim. Two embedders ship:

* `HashingEmbedder` (default) - hashed character trigrams. Offline and
  deterministic; catches surface variants ("signs" / "signing"), not meaning.
* `HTTPEmbedder` - any OpenAI-compatible `/embeddings` endpoint (OpenAI, or a
  local server such as Ollama). This is the one that understands paraphrase.

Either way, retrieval changes are gated by tests/eval/retrieval.jsonl: a
labelled question set that must keep passing in CI.
"""

from __future__ import annotations

import math
import os
import re
import zlib
from collections import Counter
from dataclasses import dataclass

import httpx

from .schemas import Claim, ClaimType, Evidence

_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-.]*")

_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "to", "of", "and",
    "or", "in", "on", "for", "with", "that", "this", "it", "as", "at", "by", "we",
    "our", "us", "do", "does", "did", "has", "have", "had", "not", "no", "if",
    "what", "which", "who", "how", "why", "when", "can", "should", "would", "will",
    # Question scaffolding: "how long", "how many" carry no subject.
    "long", "many", "much", "ever", "any", "there", "still", "one", "get", "make", "every",
    # Conversational and imperative wrappers ("tell me", "ignore previous ...").
    "tell", "show", "give", "me", "please", "explain", "describe", "list", "know",
    "ignore", "previous", "instructions",
}

#: Words that frame a question about institutional memory without naming its
#: subject. "What is our custody policy" is about custody; letting the rare
#: word "policy" dominate would rank the one document titled "...Policy" first.
_META = {"policy", "strategy", "position", "rule", "current", "currently", "plan", "stance"}

# Query-side expansion: institutional vocabulary drifts between documents
# ("custody provider" / "vendor" / "HSM"), and a founder asks in their own words.
_SYNONYMS: dict[str, list[str]] = {
    "vendor": ["provider", "custody", "coldvault", "keyforge", "supplier"],
    "custody": ["custodian", "provider", "hsm", "coldvault", "keyforge"],
    "provider": ["vendor", "custody", "coldvault", "keyforge"],
    "retention": ["retain", "retained", "logs", "years"],
    "logs": ["logging", "retention", "retained"],
    "latency": ["slow", "delay", "ms", "round-trip", "eu", "region"],
    "region": ["regional", "frankfurt", "us-east-1", "eu"],
    "eu": ["europe", "european", "frankfurt", "regional"],
    "commitment": ["committed", "promised", "agreement", "condition"],
    "promise": ["commitment", "committed", "told", "agreement"],
    "customer": ["client", "partner", "northbridge", "halcyon", "pilot"],
    "attestation": ["report", "compliance", "northbridge", "signed"],
    "outage": ["downtime", "incident", "failure", "unavailable"],
    "cost": ["spend", "price", "expensive", "budget", "finance"],
    "auth": ["authentication", "oauth", "token", "credential"],
    "sandbox": ["halcyon", "partner", "api", "access"],
    "second": ["secondary", "backup", "redundancy", "multi"],
    "add": ["onboard", "onboarded", "introduce", "adopt"],
    "switch": ["migrate", "replace", "move", "change"],
    "key": ["signing", "hsm", "custody"],
    "algorithm": ["ecdsa", "p-256", "curve", "protocol", "signing"],
    "signing": ["sign", "ecdsa", "hsm", "protocol"],
    "encryption": ["ecdsa", "signing", "key", "hsm"],
    "custodian": ["custody", "provider", "coldvault", "keyforge"],
    "keep": ["retain", "retained", "retention"],
    "record": ["log", "logged", "retained"],
    "down": ["outage", "downtime", "unavailable", "failure"],
    "partner": ["design", "halcyon", "sandbox"],
    "bank": ["northbridge"],
    "curve": ["ecdsa", "p-256"],
    "call": ["request", "client"],
    "credential": ["oauth", "token", "authenticate"],
    "approved": ["decided", "decision", "ratified", "reaffirmed"],
    "expansion": ["open", "region", "regional"],
}


def _fold(token: str) -> str:
    """Crude plural folding ("keys" -> "key", "logs" -> "log")."""
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    # rstrip: "hsm." at the end of a sentence is the same word as "hsm".
    raw = (t.rstrip(".-") for t in _TOKEN.findall(text.lower()))
    return [_fold(t) for t in raw if t not in _STOP and len(t) > 1]


_FOLDED_SYNONYMS = {_fold(k): [_fold(x) for x in v] for k, v in _SYNONYMS.items()}


def expand(tokens: list[str]) -> list[str]:
    out = list(tokens)
    for t in tokens:
        out.extend(_FOLDED_SYNONYMS.get(t, []))
    return out


Vector = dict[int, float]


def _normalise(vec: dict[int, float]) -> Vector:
    norm = math.sqrt(sum(x * x for x in vec.values())) or 1.0
    return {k: x / norm for k, x in vec.items()}


def _cosine(a: Vector, b: Vector) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(k, 0.0) for k, x in a.items())


class HashingEmbedder:
    """Offline stand-in for an embedding model: hashed character trigrams.

    ponytail: surface similarity only; a question with no shared word stems
    still misses. Configure FA_EMBEDDINGS=http for real paraphrase recall.
    """

    name = "hashing-char3"
    #: Cosine below `floor` is noise for this embedder; `weight` scales the rest
    #: into BM25 units. Both were calibrated on tests/eval/retrieval.jsonl.
    floor = 0.20
    weight = 4.0

    def __init__(self, dim: int = 4096) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[Vector]:
        out = []
        for text in texts:
            vec: Counter = Counter()
            for tok in tokenize(text):
                padded = f"#{tok}#"
                for i in range(len(padded) - 2):
                    vec[zlib.crc32(padded[i:i + 3].encode()) % self.dim] += 1.0
            out.append(_normalise(vec))
        return out


class HTTPEmbedder:
    """Any OpenAI-compatible `/embeddings` endpoint: OpenAI, or a local server."""

    floor = 0.25
    weight = 6.0

    def __init__(self, url: str, model: str, key_env: str = "") -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.key_env = key_env
        self.name = f"http:{model}"

    def embed(self, texts: list[str]) -> list[Vector]:
        key = os.environ.get(self.key_env, "") if self.key_env else ""
        r = httpx.post(f"{self.url}/embeddings",
                       headers={"Authorization": f"Bearer {key}"} if key else {},
                       json={"model": self.model, "input": texts}, timeout=30.0)
        r.raise_for_status()
        rows = sorted(r.json()["data"], key=lambda d: d["index"])
        return [_normalise(dict(enumerate(d["embedding"]))) for d in rows]


def make_embedder(kind: str, url: str = "", model: str = "", key_env: str = ""):
    if kind == "http":
        return HTTPEmbedder(url, model, key_env)
    if kind == "hashing":
        return HashingEmbedder()
    return None


@dataclass
class _Doc:
    claim: Claim
    tokens: list[str]
    tf: Counter
    length: int


class ClaimIndex:
    """BM25 over claim text, fused with an optional embedding signal."""

    K1 = 1.4
    B = 0.72

    def __init__(self, claims: list[Claim], embedder=None) -> None:
        self._docs: list[_Doc] = []
        for c in claims:
            toks = tokenize(f"{c.doc_title} {c.text}")
            self._docs.append(_Doc(c, toks, Counter(toks), len(toks)))
        self._avglen = (sum(d.length for d in self._docs) / len(self._docs)) if self._docs else 1.0
        self._df: Counter = Counter()
        for d in self._docs:
            self._df.update(set(d.tf))
        self._n = max(len(self._docs), 1)
        self.embedder = embedder
        self._vectors: list[Vector] = []
        if embedder is not None and self._docs:
            try:
                self._vectors = embedder.embed([f"{d.claim.doc_title}. {d.claim.text}"
                                                for d in self._docs])
            except Exception:  # noqa: BLE001 - an embedder outage degrades to BM25
                self.embedder = None

    def coverage(self, query: str) -> tuple[float, list[str]]:
        """Share of the question's terms the corpus knows, and the ones it does not.

        A question whose subject never appears in the corpus ("what language is
        the signing service written in") can still score well on its incidental
        words. Counting the terms nothing in the corpus mentions catches that.
        """
        tokens = list(dict.fromkeys(tokenize(query)))
        if not tokens:
            return 0.0, []
        unknown = [t for t in tokens if not self._known(t)]
        # Report the asker's word ("series"), not its folded form ("serie").
        asked = {_fold(w.rstrip(".-")): w.rstrip(".-") for w in _TOKEN.findall(query.lower())}
        return 1 - len(unknown) / len(tokens), [asked.get(t, t) for t in unknown]

    def _known(self, term: str) -> bool:
        if self._df.get(term) or any(self._df.get(x) for x in expand([term])[1:]):
            return True
        # "sign" / "signing", "onboard" / "onboarded": same stem, other ending.
        return len(term) >= 4 and any(v.startswith(term) or (len(v) >= 4 and term.startswith(v))
                                      for v in self._df)

    @property
    def claims(self) -> list[Claim]:
        return [d.claim for d in self._docs]

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log(1 + (self._n - df + 0.5) / (df + 0.5))

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        type_filter: set[ClaimType] | None = None,
        doc_filter: set[str] | None = None,
    ) -> list[Evidence]:
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        weighted: Counter = Counter()
        for t in q_tokens:
            weighted[t] += 0.3 if t in _META else 1.0
        for t in expand(q_tokens):
            if t not in weighted:
                weighted[t] += 0.45  # expansion terms count less than the user's own words

        dense = self._dense(query)
        scored: list[tuple[float, _Doc]] = []
        for i, d in enumerate(self._docs):
            if type_filter and d.claim.claim_type not in type_filter:
                continue
            if doc_filter and d.claim.doc_id not in doc_filter:
                continue
            score = 0.0
            for term, weight in weighted.items():
                f = d.tf.get(term, 0)
                if not f:
                    continue
                denom = f + self.K1 * (1 - self.B + self.B * d.length / self._avglen)
                score += weight * self._idf(term) * (f * (self.K1 + 1)) / denom
            if dense:
                score += self.embedder.weight * max(0.0, dense[i] - self.embedder.floor)
            if score <= 0:
                continue
            score *= self._structural_boost(d.claim, set(weighted))
            scored.append((score, d))

        scored.sort(key=lambda pair: (-pair[0], pair[1].claim.claim_id))
        return [
            Evidence(
                claim_id=d.claim.claim_id,
                doc_id=d.claim.doc_id,
                doc_title=d.claim.doc_title,
                source=d.claim.doc_source,
                date=d.claim.date,
                claim_type=d.claim.claim_type,
                text=d.claim.text,
                score=round(score, 4),
                flags=list(d.claim.injection_flags),
            )
            for score, d in scored[:top_k]
        ]

    def _dense(self, query: str) -> list[float]:
        if self.embedder is None or not self._vectors:
            return []
        try:
            q = self.embedder.embed([query])[0]
        except Exception:  # noqa: BLE001
            return []
        return [_cosine(q, v) for v in self._vectors]

    @staticmethod
    def _structural_boost(claim: Claim, query_terms: set[str]) -> float:
        boost = 1.0
        boost += 0.08 * len(set(claim.topics) & query_terms)
        if claim.claim_type is ClaimType.DECISION:
            boost += 0.12          # governing records answer more questions
        if claim.injection_flags:
            boost -= 0.35          # never let suspicious text win on rank alone
        return max(boost, 0.1)
