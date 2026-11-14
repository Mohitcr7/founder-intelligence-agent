"""Category discipline: the labels must not drift, in either direction."""

from founder_agent.schemas import ClaimType


def _claim(system, claim_id):
    return next(c for c in system.claims if c.claim_id == claim_id)


def test_explicit_decision_is_a_decision(system):
    assert _claim(system, "DOC-01#c1").claim_type is ClaimType.DECISION
    assert _claim(system, "DOC-14#c1").claim_type is ClaimType.DECISION


def test_self_labelled_hypothesis_stays_a_hypothesis(system):
    for cid in ("DOC-03#c1", "DOC-13#c1"):
        assert _claim(system, cid).claim_type is ClaimType.HYPOTHESIS


def test_reporting_a_hypothesis_is_not_holding_one(system):
    """DOC-07 cites DOC-03's hypothesis while drawing a conclusion from data."""
    claim = _claim(system, "DOC-07#c3")
    assert claim.claim_type is ClaimType.INFERENCE


def test_recommendation_is_never_promoted_to_decision(system):
    """DOC-06 recommends and explicitly defers; DOC-07 says 'not a decision'."""
    for cid in ("DOC-06#c2", "DOC-06#c3", "DOC-07#c3"):
        assert _claim(system, cid).claim_type is not ClaimType.DECISION


def test_superseded_document_yields_no_governing_decision(system):
    doc17 = [c for c in system.claims if c.doc_id == "DOC-17"]
    assert all(c.claim_type is not ClaimType.DECISION for c in doc17)


def test_every_claim_carries_its_reason(system):
    assert all(c.type_reason for c in system.claims)


#: One sentence per rule, each of which must land on exactly that rule - which
#: also pins the precedence order. Adding a rule without an example fails below.
RULE_EXAMPLES = {
    "self-labelled hypothesis": "Hypothesis: the queue is the bottleneck.",
    "explicitly unvalidated": "The new cache path is untested.",
    "author disclaims confirmation": "The migration date is not confirmed.",
    "first-person speculation": "I suspect the queue is slow.",
    "speculative conditional": "If true, the rollout slips a quarter.",
    "explicit 'Decision:' marker": "Decision: we adopt Postgres for the ledger.",
    "ratification verb": "The board approved the budget.",
    "binding commitment language": "We will use Vault as the sole secrets store going forward.",
    "effectivity clause": "The change freeze applies effective immediately.",
    "forward-binding directive": "The legacy cluster should be wound down.",
    "evidence-to-conclusion link": "This supports moving the ledger to Postgres.",
    "causal attribution": "Root cause: a full disk on the primary.",
    "recommendation, not a decision": "Finance recommends a vendor review.",
    "normative generalisation": "Industry best practice is to rotate keys.",
    "historical record of a superseded position": "[SUPERSEDED by DOC-1] Old text here.",
    "document status marker": "Status: DRAFT",
    "reported past event": "The quarterly report was delivered.",
    "reported current state": "The export API is rate-limited.",
    "stated system property": "All admin writes are logged.",
    "quantified measurement": "Throughput peaked at 900 requests.",
    "causal reasoning": "Latency rose because of retries.",
    "hedged conclusion": "The cache likely helps.",
}


def test_every_rule_has_an_example_that_hits_it_first():
    from founder_agent.classify import RULES, Classifier
    labels = [r.label for r in RULES]
    assert sorted(labels) == sorted(RULE_EXAMPLES), "add an example for every new rule"
    for rule in RULES:
        ctype, reason, _ = Classifier().classify(RULE_EXAMPLES[rule.label])
        assert (ctype, reason) == (rule.claim_type, rule.label), rule.label


def test_veto_beats_binding_language():
    from founder_agent.classify import Classifier
    ctype, _, _ = Classifier().classify(
        "We will use KeyForge going forward - this is still a recommendation, not a decision.")
    assert ctype is not ClaimType.DECISION


def test_superseded_document_demotes_its_decisions():
    from founder_agent.ingest import CorpusLoader, apply_supersession
    old = CorpusLoader([]).parse(
        "---\ndoc_id: DOC-60\ntitle: Old\ndoc_type: decision\ndeclared_status: SUPERSEDED\n---\n\n"
        "Decision: logs are kept for 1 year.\n", {"doc_id": "DOC-60", "channel": "founder"})
    assert old.claims[0].claim_type is ClaimType.DECISION      # as written
    apply_supersession([old])
    assert old.claims[0].claim_type is ClaimType.FACT
    assert "demoted from DECISION" in old.claims[0].type_reason


def test_supersession_by_a_later_record_demotes_the_earlier_one():
    from founder_agent.ingest import CorpusLoader, apply_supersession
    loader = CorpusLoader([])
    old = loader.parse("---\ndoc_id: DOC-61\ntitle: Old\ndoc_type: decision\n---\n\n"
                       "Decision: logs are kept for 1 year.\n",
                       {"doc_id": "DOC-61", "channel": "founder"})
    new = loader.parse("---\ndoc_id: DOC-62\ntitle: New\ndoc_type: decision\nsupersedes: [DOC-61]\n"
                       "---\n\nDecision: logs are kept for 5 years.\n",
                       {"doc_id": "DOC-62", "channel": "founder"})
    apply_supersession([old, new])
    assert old.claims[0].claim_type is ClaimType.FACT
    assert new.claims[0].claim_type is ClaimType.DECISION
