"""Corpus loading: markdown + frontmatter -> Documents -> Claims.

Claims, not documents, are the unit of retrieval and citation. A document can
hold a decision and an untested hypothesis in adjacent sentences; citing the
document would blur them together.

Provenance is stamped here from the intake record (`provenance.jsonl`, written
by whoever received the file), never from the file's own header. A document
that says `authority: board` but arrived as a manual upload is an upload.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from .classify import Classifier
from .config import TRUST_REGISTRY
from .guards import scan_for_injection
from .schemas import Authority, Claim, ClaimType, Document

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
DOC_ID = re.compile(r"^[A-Z]+-\d+$")
PROVENANCE_FILE = "provenance.jsonl"

# Sentence splitter that keeps "ColdVault Inc.", "OAuth 2.0", "P-256",
# "SOC 2 Type II" and "us-east-1" intact.
_ABBREV = r"(?<!\bInc)(?<!\bLtd)(?<!\bCo)(?<!\be\.g)(?<!\bi\.e)(?<!\bNo)(?<!\bvs)"
_SENT_SPLIT = re.compile(rf"{_ABBREV}(?<=[.!?])\s+(?=[A-Z\[])")

_MONTHS = ("january february march april may june july august september october "
           "november december").split()
_EVENT_DATE = re.compile(
    rf"\b({'|'.join(_MONTHS)})\s+(\d{{1,2}}),\s*(\d{{4}})\b|\b(\d{{4}})-(\d{{2}})-(\d{{2}})\b", re.I)
_APPROVER = re.compile(r"\b(founder|ceo|board)\b", re.I)

#: "by May 3, 2026" is a deadline, not the date something happened.
_DEADLINE_LEAD = re.compile(r"\b(by|until|before|starting|from|no later than)\s*$", re.I)


def _parse_scalar(raw: str):
    raw = raw.strip()
    if raw in {"null", "~", ""}:
        return None
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [p.strip().strip('"\'') for p in inner.split(",")]
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return datetime.strptime(raw, "%Y-%m-%d").date()
    return raw


def _parse_frontmatter(block: str) -> dict:
    out: dict = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = _parse_scalar(value)
    return out


def split_sentences(paragraph: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(paragraph.strip()) if p.strip()]
    return parts or ([paragraph.strip()] if paragraph.strip() else [])


def event_date(sentence: str) -> date | None:
    """The date a sentence says something happened on, if it names one."""
    for m in _EVENT_DATE.finditer(sentence):
        if _DEADLINE_LEAD.search(sentence[:m.start()]):
            continue
        try:
            if m.group(1):
                return date(int(m.group(3)), _MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)))
            return date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        except ValueError:
            continue
    return None


def _flags(sentence: str, authority: Authority) -> list[str]:
    """Injection heuristics, read in light of who actually filed the text.

    "The board approved X" is forgery when an upload says it and the record
    itself when the board connector delivers it.
    """
    flags = scan_for_injection(sentence)
    named = {m.lower().replace("ceo", "founder") for m in _APPROVER.findall(sentence)}
    if "authority_forgery" in flags and named == {authority.value}:
        flags.remove("authority_forgery")
    return flags


def read_provenance(directory: Path) -> dict[str, dict]:
    path = directory / PROVENANCE_FILE
    if not path.exists():
        return {}
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {r["doc_id"]: r for r in records}     # last record for an id wins


def peek_doc_id(raw: str) -> str | None:
    m = _FRONTMATTER.match(raw)
    if not m:
        return None
    return str(_parse_frontmatter(m.group(1)).get("doc_id") or "") or None


class CorpusLoader:
    def __init__(self, corpus_dirs: Path | list[Path], classifier: Classifier | None = None) -> None:
        self.corpus_dirs = [corpus_dirs] if isinstance(corpus_dirs, Path) else list(corpus_dirs)
        self.classifier = classifier or Classifier()

    def load(self) -> list[Document]:
        docs: dict[str, Document] = {}
        for directory in self.corpus_dirs:
            if not directory.exists():
                continue
            intake = read_provenance(directory)
            for path in sorted(directory.glob("*.md")):
                doc = self.parse(path.read_text(encoding="utf-8"), intake.get(path.stem),
                                 fallback_id=path.stem)
                # Ids are filenames. A file claiming another document's id
                # would otherwise borrow its provenance or shadow it.
                if doc is None or doc.doc_id != path.stem or doc.doc_id in docs:
                    continue
                docs[doc.doc_id] = doc
        out = list(docs.values())
        apply_supersession(out)
        return out

    def parse(self, raw: str, record: dict | None, fallback_id: str = "") -> Document | None:
        m = _FRONTMATTER.match(raw)
        if not m:
            return None
        meta = _parse_frontmatter(m.group(1))
        body = raw[m.end():].strip()

        doc = Document(
            doc_id=str(meta.get("doc_id") or fallback_id),
            title=str(meta.get("title") or fallback_id),
            doc_type=str(meta.get("doc_type") or "note"),
            source=str(meta.get("source") or fallback_id),
            date=meta.get("date") if isinstance(meta.get("date"), date) else None,
            authority=Authority.UNKNOWN,
            topics=list(meta.get("topics") or []),
            declared_status=meta.get("declared_status"),
            supersedes=list(meta.get("supersedes") or []),
            superseded_by=meta.get("superseded_by"),
            body=body,
            declared_authority=str(meta["authority"]) if meta.get("authority") else None,
        )
        self._stamp_provenance(doc, record)
        doc.claims = self._to_claims(doc)
        self._propagate_document_trust(doc)
        return doc

    @staticmethod
    def _stamp_provenance(doc: Document, record: dict | None) -> None:
        channel = (record or {}).get("channel")
        if channel not in TRUST_REGISTRY:
            channel = "upload"
        row = TRUST_REGISTRY[channel]
        doc.channel = channel
        doc.authority = Authority(row["authority"])
        doc.can_govern = row["can_govern"]
        doc.quarantined = row["quarantine"]
        doc.filed_by = (record or {}).get("filed_by", "")
        received = (record or {}).get("received_at")
        doc.received_at = datetime.fromisoformat(received) if received else None
        # A release class can be widened only by the intake record (where a
        # compliance clearance is filed), never by the document itself.
        doc.sensitivity = (record or {}).get("sensitivity") or row["sensitivity"]
        if doc.declared_authority and doc.declared_authority != doc.authority.value:
            doc.provenance_notes.append(
                f"declares authority '{doc.declared_authority}' but arrived through the "
                f"'{channel}' channel ({doc.authority.value}); the declared value is ignored")
        if doc.quarantined:
            doc.provenance_notes.append(
                f"quarantined: '{channel}' vouches for no author; visible as data, cannot govern "
                f"until it is refiled through a trusted channel")

    @staticmethod
    def _propagate_document_trust(doc: Document) -> None:
        """Trust is a property of the source, not of the sentence.

        If one passage in a document is trying to issue instructions, the
        document is attacker-influenced and its *other* sentences are no more
        trustworthy than that one - an attacker who can write paragraph three
        can write paragraph four. So the flag propagates to the whole document
        and the quarantine covers all of it. Unverified provenance quarantines
        the same way.
        """
        triggered = sorted({f for c in doc.claims for f in c.injection_flags})
        for claim in doc.claims:
            if triggered and not claim.injection_flags:
                claim.injection_flags = [f"tainted_source:{doc.doc_id}"]
            if doc.quarantined:
                claim.injection_flags.append(f"unverified_provenance:{doc.channel}")

    def _to_claims(self, doc: Document) -> list[Claim]:
        claims: list[Claim] = []
        ordinal = 0
        for paragraph in [p for p in doc.body.split("\n\n") if p.strip()]:
            for sentence in split_sentences(paragraph):
                ordinal += 1
                ctype, reason, conf = self.classifier.classify(sentence, doc_type=doc.doc_type)
                claims.append(
                    Claim(
                        claim_id=f"{doc.doc_id}#c{ordinal}",
                        doc_id=doc.doc_id,
                        ordinal=ordinal,
                        text=sentence,
                        claim_type=ctype,
                        type_reason=reason,
                        type_confidence=conf,
                        topics=doc.topics,
                        date=event_date(sentence) or doc.date,
                        authority=doc.authority,
                        doc_title=doc.title,
                        doc_source=doc.source,
                        injection_flags=_flags(sentence, doc.authority),
                        sensitivity=doc.sensitivity,
                    )
                )
        return claims


def apply_supersession(documents: list[Document]) -> None:
    """A superseded record holds no governing decision, whatever it says.

    Its DECISION sentences become FACT: it is a fact that the decision was
    recorded, and it is no longer in force. Only a governable document can
    supersede another, so a quarantined upload naming DOC-10 changes nothing.
    """
    superseded = {d.doc_id for d in documents if d.declared_status == "SUPERSEDED" or d.superseded_by}
    superseded |= {old for d in documents if d.governable for old in d.supersedes}
    for doc in documents:
        if doc.doc_id not in superseded:
            continue
        for claim in doc.claims:
            if claim.claim_type is ClaimType.DECISION:
                claim.claim_type = ClaimType.FACT
                claim.type_reason = (f"{claim.type_reason}; demoted from DECISION: {doc.doc_id} is "
                                     f"superseded - a historical record, not a governing one")
