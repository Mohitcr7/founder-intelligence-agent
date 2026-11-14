"""Contradictions: find the real ones, explain them, and skip the false ones."""


def test_finds_the_custody_contradiction(system):
    clusters = system.detector.cluster()
    custody = next(c for c in clusters if c.axis == "custody_vendor_cardinality")
    assert custody.severity == "high"
    assert {"DOC-01", "DOC-02", "DOC-10"} <= set(custody.documents)


def test_explanation_is_semantic_not_lexical(system):
    custody = next(c for c in system.detector.cluster()
                   if c.axis == "custody_vendor_cardinality")
    why = custody.primary.why.lower()
    assert "cardinality" in why or "exclusiv" in why
    assert "cannot both" in why
    # It must not merely observe that the wording differs.
    assert "different words" not in why.replace("not different words for one arrangement", "")


def test_governance_gap_is_its_own_finding(system):
    kinds = {c.primary.kind for c in system.detector.cluster()}
    assert "unratified_change" in kinds


def test_commitment_slip_detected(system):
    cluster = next(c for c in system.detector.cluster()
                   if c.primary.kind == "commitment_breach")
    assert {"DOC-15", "DOC-16"} == set(cluster.documents)


def test_fulfilled_commitment_is_not_a_contradiction(system):
    """DOC-05 promised attestation; DOC-11 delivered it on time."""
    pairs = {(c.claim_a.doc_id, c.claim_b.doc_id)
             for cl in system.detector.cluster()
             for c in [cl.primary, *cl.supporting]}
    assert ("DOC-05", "DOC-11") not in pairs
    assert ("DOC-11", "DOC-05") not in pairs


def test_hypothesis_does_not_contradict_a_decision(system):
    """DOC-03 is speculation; it must not be reported as conflicting with policy."""
    for cluster in system.detector.cluster():
        for c in [cluster.primary, *cluster.supporting]:
            types = {c.claim_a.claim_type.value, c.claim_b.claim_type.value}
            assert not (types & {"HYPOTHESIS"}), f"{c.contradiction_id} uses a hypothesis"


def test_every_cluster_states_what_governs_today(system):
    for cluster in system.detector.cluster():
        assert cluster.current_position


def test_explicit_supersession_doc14_over_doc17(system):
    cluster = next(c for c in system.detector.cluster() if c.primary.kind == "explicit_supersession")
    assert set(cluster.documents) == {"DOC-14", "DOC-17"}
    assert cluster.primary.resolution.startswith("DOC-14 governs")
    assert cluster.severity == "low"          # real, but already resolved in the record


def test_external_note_diverges_from_the_board_decision(system):
    cluster = next(c for c in system.detector.cluster() if c.primary.kind == "external_divergence")
    assert {"DOC-08", "DOC-10"} <= set(cluster.documents)
    assert "does not bind" in cluster.primary.why        # an industry note is not policy


def _claim(cid, text, ctype, topics):
    from founder_agent.schemas import Authority, Claim, ClaimType
    return Claim(claim_id=cid, doc_id=cid.split("#")[0], ordinal=1, text=text,
                 claim_type=ClaimType(ctype), type_reason="test", topics=topics,
                 authority=Authority.FOUNDER, doc_title="Retention")


def test_same_axis_different_subject_is_not_a_contradiction():
    from founder_agent.contradictions import ContradictionDetector
    claims = [
        _claim("DOC-1#c1", "Decision: All signing request logs will be retained for 7 years.",
               "DECISION", ["retention", "logs", "policy"]),
        _claim("DOC-2#c1", "Decision: Support tickets will be retained for 2 years.",
               "DECISION", ["retention", "logs", "policy"]),
    ]
    assert ContradictionDetector(claims).detect_all() == []


def test_same_axis_same_subject_still_conflicts():
    from founder_agent.contradictions import ContradictionDetector
    claims = [
        _claim("DOC-1#c1", "Decision: All signing request logs will be retained for 7 years.",
               "DECISION", ["retention", "logs", "policy"]),
        _claim("DOC-2#c1", "Decision: Signing request logs will be retained for 2 years.",
               "DECISION", ["retention", "logs", "policy"]),
    ]
    found = ContradictionDetector(claims).detect_all()
    assert found and found[0].subject in {"signing_logs", ""}


def test_quarantined_text_cannot_be_one_side_of_a_contradiction(poisoned_corpus, make_settings):
    from founder_agent.app import build_system
    poisoned = build_system(make_settings(poisoned_corpus))
    docs = {d for cl in poisoned.detector.cluster() for d in cl.documents}
    assert "DOC-99" not in docs
