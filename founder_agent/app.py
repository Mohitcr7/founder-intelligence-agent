"""Assemble the system, and rebuild its derived state when a document arrives."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace

from .agents.orchestrator import Orchestrator
from .audit import AuditLog
from .authorization import ActionGateway, sensitivity_policy
from .config import TRUST_REGISTRY, Settings, settings as default_settings
from .classify import Classifier
from .contradictions import ContradictionDetector
from .decisions import DecisionLedger
from .ingest import DOC_ID, PROVENANCE_FILE, CorpusLoader, apply_supersession, peek_doc_id
from .knowledge import KnowledgeService
from .llm.registry import ProviderRegistry
from .retrieval import ClaimIndex, make_embedder
from .schemas import ClaimType, Document, utcnow
from .tools.external import register_default_tools
from .tools.registry import ToolRegistry

MAX_INGEST_BYTES = 256 * 1024


class IngestError(ValueError):
    pass


class FounderAgentSystem:
    """Long-lived pieces (providers, tools, audit, gateway) plus everything
    derived from the corpus, which `reindex` swaps in one step."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = ProviderRegistry(settings)
        self.audit = AuditLog(settings.audit_path, settings.audit_anchor_path,
                              settings.audit_anchor_every)
        self.tools = register_default_tools(ToolRegistry())
        self.gateway = ActionGateway(self.tools, self.audit, settings,
                                     content_policy=sensitivity_policy(lambda: self.claims))
        self._classifier = Classifier()
        self._embedder = make_embedder(settings.embeddings, settings.embeddings_url,
                                       settings.embeddings_model, settings.embeddings_key_env)
        self._lock = threading.Lock()
        loader = CorpusLoader([settings.corpus_dir, settings.intake_dir], self._classifier)
        self.reindex(loader.load())

    def reindex(self, documents: list[Document]) -> None:
        """Rebuild index, ledger, detector and agents from a document list.

        ponytail: a full rebuild per new document - fine for hundreds of
        documents; past that, update the index and ledger incrementally.
        """
        claims = [c for d in documents for c in d.claims]
        index = ClaimIndex(claims, self._embedder)
        ledger = DecisionLedger(documents)
        detector = ContradictionDetector(claims, {d.doc_id: d for d in documents}, ledger)
        knowledge = KnowledgeService(index, ledger, self.registry, self.settings)
        self.tools.register(replace(self.tools.get("corpus.search"),
                                    handler=_corpus_search(index)))
        orchestrator = Orchestrator(index, ledger, knowledge, detector, self.registry,
                                    self.audit, self.gateway, self.tools)
        (self.documents, self.claims, self.index, self.ledger, self.detector,
         self.knowledge, self.orchestrator) = (
            documents, claims, index, ledger, detector, knowledge, orchestrator)

    # ------------------------------------------------------------------
    def ingest(self, markdown: str, *, channel: str, filed_by: str) -> Document:
        """Add one document without a restart. Provenance comes from `channel`.

        The caller (the API) has already authenticated the channel's connector
        credential; an `upload` needs none because it lands in quarantine.
        """
        if channel not in TRUST_REGISTRY:
            raise IngestError(f"unknown intake channel '{channel}'")
        if len(markdown.encode("utf-8")) > MAX_INGEST_BYTES:
            raise IngestError("document exceeds the ingest size limit")
        doc_id = peek_doc_id(markdown)
        if not doc_id or not DOC_ID.match(doc_id):
            raise IngestError("frontmatter must carry a doc_id of the form ABC-123")

        with self._lock:
            if any(d.doc_id == doc_id for d in self.documents):
                # History is append-only: a new version is a new document that
                # supersedes the old one, never an overwrite.
                raise IngestError(f"{doc_id} already exists; file a new document that supersedes it")
            record = {"doc_id": doc_id, "channel": channel, "filed_by": filed_by,
                      "received_at": utcnow().isoformat(),
                      "sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest()}
            doc = CorpusLoader([], self._classifier).parse(markdown, record, fallback_id=doc_id)
            if doc is None:
                raise IngestError("document has no frontmatter block")

            intake = self.settings.intake_dir
            intake.mkdir(parents=True, exist_ok=True)
            (intake / f"{doc_id}.md").write_text(markdown, encoding="utf-8")
            with (intake / PROVENANCE_FILE).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")

            documents = self.documents + [doc]
            apply_supersession(documents)
            self.reindex(documents)

        self.audit.record("document_ingested", trace_id=f"ingest:{doc_id}", actor=filed_by, payload={
            **record, "authority": doc.authority.value, "declared_authority": doc.declared_authority,
            "can_govern": doc.can_govern, "quarantined": doc.quarantined,
            "governs": doc_id in self.ledger.entries, "notes": doc.provenance_notes,
        })
        return doc

    # ------------------------------------------------------------------
    def attention(self) -> dict[str, list[dict]]:
        """What only a human can close, sorted into three queues.

        Approve - an irreversible action is ready. Decide - the record is split
        or a promise slipped. FYI - logged, not urgent. Nothing else reaches the
        founder: no connector noise, no requests to define new axes.
        """
        approve = [{"title": a.intent, "action_id": a.action_id, "sources": a.supporting_claim_ids,
                    "why": "; ".join(a.warnings) or "ready for a human decision"}
                   for a in self.gateway.pending()]
        decide, fyi = [], []
        for cl in self.detector.cluster():
            item = {"title": cl.title, "why": cl.primary.why, "sources": cl.documents,
                    "governs_today": cl.current_position}
            settled = cl.primary.kind in {"explicit_supersession", "external_divergence"}
            (fyi if settled or cl.severity == "low" else decide).append(item)
        for c in self.claims:
            if c.claim_type is ClaimType.INFERENCE and "recommendation" in c.type_reason \
                    and not c.injection_flags:
                fyi.append({"title": f"{c.doc_title}: recommendation, not a decision",
                            "why": c.text, "sources": [c.claim_id], "governs_today": ""})
        return {"approve": approve, "decide": decide, "fyi": fyi}


def _corpus_search(index: ClaimIndex):
    """Read-only search over the live index. Quarantined text is never returned."""
    def handler(payload: dict, idempotency_key: str = "") -> dict:
        types = {ClaimType(t) for t in payload.get("types", [])} or None
        docs = set(payload.get("doc_ids", [])) or None
        hits = index.search(str(payload.get("query", "")), top_k=int(payload.get("top_k", 5)),
                            type_filter=types, doc_filter=docs)
        return {"results": [{"claim_id": e.claim_id, "doc_id": e.doc_id, "text": e.text,
                             "claim_type": e.claim_type.value, "score": e.score}
                            for e in hits if not e.flags]}
    return handler


def build_system(settings: Settings | None = None) -> FounderAgentSystem:
    settings = settings or default_settings
    settings.validate()
    settings.ensure_dirs()
    return FounderAgentSystem(settings)
