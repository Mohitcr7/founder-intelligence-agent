# Known limitations and next engineering steps

Written after the recommended modifications (README → *Recommended modifications
implemented*). What those changes closed is listed there; what they leave open is
listed here.

## Known limitations

**1. The default embedder is a hashing embedder, not a semantic one.** Hybrid
retrieval fuses BM25 with a dense score. Offline, the dense half is a
character-trigram hashing embedder. It catches morphology and spelling variants
("onboarded" / "onboarding"), but it does not know that "who holds our keys" means
"custody provider". That question still finds DOC-10 only because decision lineage
pulls in the governing record. A real embedding model is one setting away
(`FA_EMBEDDINGS=http`, any OpenAI-compatible endpoint), but it has not been measured
on the eval set here. The 64-question eval set and its recall/MRR floors were
measured on this one 18-document corpus. They guard against regression. They do not
show the system generalises.

**2. Provenance is per channel, not signed per document.** Authority comes from the
intake channel, and each trusted channel needs its own connector credential. Anyone
holding that credential is trusted as that channel, though. No per-document
signature exists, and the seed intake record (`corpus/provenance.jsonl`) is a file
anyone with repository write access can edit. A quarantined upload cannot be cleared
in place: it gains authority only when it is refiled through a trusted channel, and
there is no review workflow for that yet.

**3. Approver identity is a shared secret.** Approve, deny and execute all need the
approver credential, so an agent or an API-only caller cannot act. Every person who
holds the credential looks the same to the system, though, and `X-Approver-Id` is
self-declared. "Approved by founder" in the audit log therefore says who *claimed*
to approve. It does not authenticate them. The API credential is one shared token
too. SSO / per-person identity was deferred on purpose (review item 4.4.3).

**4. The audit anchor is only as strong as where it is stored.** Chain heads are
written to `FA_AUDIT_ANCHOR` every 25 records, and immediately whenever an action
executes or a document is ingested. By default that is a local file, which a
process able to rewrite the log could also rewrite. The protection is real only when
the path is on storage this process cannot overwrite (an object-lock bucket or an
append-only mount). Records written after the most recent anchor (up to 24, such as
approvals or refusals) can still be truncated without detection.

**5. Injection heuristics are a filter, not a boundary.** A plausible poisoned
document that trips no heuristic still enters retrieval. The architectural controls
are what hold: document text never reaches the planner, nothing in a document can
pick a tool, an upload cannot govern, and consequential acts need human approval.
The heuristics give detection and taint propagation. They do not give immunity.

**6. Sensitivity is per document, and the leak check is near-verbatim.** Claims take
their release class from their channel or intake record. Labels are not set per
field. The gateway refuses internal claims that are cited, and internal text pasted
in word for word. A model that *paraphrases* an internal claim into a draft
gets past the text check. The draft path is protected anyway, because it is built
in code from shareable claims only, but a free-form drafting tool would not be. No
per-caller access control exists either: any holder of the API credential reads
every claim.

**7. Stance axes and subjects are declarative.** `stance.py` encodes the axes, and
since 4.7 it also keys each position by subject, so the same axis about different
things no longer conflicts. Subjects are found with keyword rules, though. A
conflict on a family of propositions that has no axis is caught only by the generic
numeric, commitment and governance-gap detectors. The intended next step is axes the
model *proposes* and ops accepts, with the comparison staying deterministic.

**8. Event dates are pattern-matched.** A claim is dated by the explicit date it
names, falling back to its document's date. Deadlines are excluded on purpose.
Relative phrases ("last sprint", "two weeks later") are not resolved.

**9. Single process, rebuilt on ingest.** Live ingest rebuilds the index, the ledger
and the contradiction clusters in-process. This is marked `ponytail:` in
`app.py`: fine for hundreds of documents, not for tens of thousands. The action
store is JSONL with fsync and a single writer. There is no horizontal scaling.

**10. The local model path is wired but not field-tested.** `FA_LOCAL_URL` registers
an OpenAI-compatible provider, and a test confirms the registration. It has not been
run against a real Ollama or vLLM server. Structured synthesis depends on the model
following the JSON schema. If a model ignores it, the system falls back to checking
prose. If nothing survives citation validation, it falls back to quoting sources.

**11. The deterministic path composes extractively.** With no API key, answers are
the source sentences with their labels and citations. That is honest and cannot
hallucinate, but it is not synthesis.

---

## Recommended next three engineering steps

### 1. Real identity at every boundary
Put SSO/OIDC in front of the API and the approval console, so each approval is
signed by a named person rather than by whoever holds a shared secret. Replace
connector credentials with per-connector signing (an HMAC over each document, or
mTLS) so provenance can be checked per document. Add a review workflow that lets a
named person release a quarantined upload, with the release in the audit log.

*Why first:* identity is now the weakest link in every control that matters. The
gateway, the channels and the audit log already record *which* principal acted.
They just cannot yet prove it.

### 2. Durable stores with an anchor the process cannot touch
Move `audit.jsonl` and `actions.jsonl` into a transactional append-only store, point
the anchor at object-lock storage, and run a scheduled job that checks the chain and
alerts on a break. Make the index and ledger update incrementally on ingest instead
of rebuilding.

*Why second:* the logic is done (at-most-once execute, anchors, reconstruction of
denials). What remains is putting it on storage that keeps those guarantees under
real operations.

### 3. Measure retrieval on real questions
Run the eval set against a real embedding model behind `FA_EMBEDDINGS=http`. Grow
the set from the founder's actual questions: log every UNKNOWN and every answer the
founder corrects. Add a matching labelled set for contradiction recall.

*Why third:* the floors in `tests/test_retrieval_eval.py` prevent regression on this
corpus. Only real questions show whether the system misses what matters.

---

### Deliberately not next

- **A larger agent swarm.** More subagents is not more judgement. The three that
  exist (research, red team, technical) cover the kinds of question this corpus
  holds.
- **Fine-tuning a classifier.** The rule-based classifier is auditable: every label
  carries the trigger that produced it, and every rule has an example that must hit
  it first. Giving that up for a few points of accuracy on a 46-claim corpus is a bad
  trade in a system whose value is that it can be checked.
- **Nicer UI.** Out of scope. The console already exposes every control path,
  including the approval queue, provenance and the audit chain.
