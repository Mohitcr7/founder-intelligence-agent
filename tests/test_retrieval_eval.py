"""Retrieval quality gate over a labelled question set (tests/eval/retrieval.jsonl).

Any change to tokenising, synonyms, weights, the embedder or the UNKNOWN floor
has to keep this passing. Kinds:

* direct / paraphrase - must answer, and the answer's evidence must hold every
  required claim id (the full pipeline: quarantine, decision lineage, floor).
* adversarial - must lead with the governing record and surface the listed ids.
* unknown - not in the corpus; must say UNKNOWN rather than guess.

The raw index is also scored (recall@8, MRR) against a ratchet: raise the
floor when retrieval improves, never lower it to make a change pass.
"""

import json

import pytest

from founder_agent.config import ROOT

ROWS = [json.loads(line) for line in
        (ROOT / "tests" / "eval" / "retrieval.jsonl").read_text().splitlines() if line.strip()]
ANSWERABLE = [r for r in ROWS if r["kind"] in {"direct", "paraphrase"}]

RECALL_FLOOR = 0.96     # hybrid (hashing) measured 51/53; BM25 alone 50/53
MRR_FLOOR = 0.84        # hybrid measured 0.849; BM25 alone 0.817


def test_eval_set_is_big_enough_to_mean_something():
    assert len(ROWS) >= 60
    assert {r["kind"] for r in ROWS} == {"direct", "paraphrase", "adversarial", "unknown"}


@pytest.mark.parametrize("row", ANSWERABLE, ids=lambda r: r["q"][:50])
def test_answerable_question_is_answered_from_the_right_claims(system, row):
    answer = system.knowledge.answer(row["q"])
    assert answer.verdict != "UNKNOWN", answer.missing
    missing = set(row["expect"]) - {e.claim_id for e in answer.evidence}
    assert not missing, f"required evidence not surfaced: {sorted(missing)}"


@pytest.mark.parametrize("row", [r for r in ROWS if r["kind"] == "adversarial"],
                         ids=lambda r: r["q"][:50])
def test_adversarial_question_leads_with_what_governs(system, row):
    answer = system.knowledge.answer(row["q"])
    assert answer.segments, answer.missing
    assert answer.segments[0].citations[0].split("#")[0] == row["lead_doc"]
    assert set(row["evidence"]) <= {e.claim_id for e in answer.evidence}


@pytest.mark.parametrize("row", [r for r in ROWS if r["kind"] == "unknown"],
                         ids=lambda r: r["q"][:50])
def test_out_of_corpus_question_is_unknown(system, row):
    assert system.knowledge.answer(row["q"]).verdict == "UNKNOWN"


def test_raw_index_quality_does_not_regress(system):
    hits, reciprocal = 0, 0.0
    for row in ANSWERABLE:
        got = [e.claim_id for e in system.index.search(row["q"], top_k=8)]
        hits += set(row["expect"]) <= set(got)
        reciprocal += sum(1 / (got.index(c) + 1) for c in row["expect"] if c in got) / len(row["expect"])
    recall, mrr = hits / len(ANSWERABLE), reciprocal / len(ANSWERABLE)
    assert recall >= RECALL_FLOOR, f"recall@8 {recall:.3f} < {RECALL_FLOOR}"
    assert mrr >= MRR_FLOOR, f"MRR {mrr:.3f} < {MRR_FLOOR}"


def test_retrieval_misses_cannot_resurrect_an_old_rule(system):
    """Search finds only the founder memo; the board record must still govern."""
    lookup = system.ledger.lookup("What is our custody vendor strategy?", ["DOC-01"])
    assert lookup.governing.doc_id == "DOC-10"
    # "Who holds our keys?" never retrieves DOC-10 by itself (it never says "key").
    answer = system.knowledge.answer("Who holds our keys?")
    assert "DOC-10#c1" not in {e.claim_id for e in system.index.search("Who holds our keys?")}
    assert "DOC-10#c1" in {e.claim_id for e in answer.evidence}
    assert not answer.segments[0].citations[0].startswith("DOC-01")
