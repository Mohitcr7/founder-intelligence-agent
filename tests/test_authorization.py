"""The human-authorization boundary, attacked from every direction we can reach."""

from datetime import timedelta

import pytest

from founder_agent.authorization import ActionGateway, AuthorizationError, Principal
from founder_agent.schemas import ActionStatus, utcnow


def _human(system, id_="founder"):
    return Principal(kind="human", id=id_, token=system.settings.approver_token)


def _prepare(system, payload=None, gateway=None):
    return (gateway or system.gateway).prepare(
        principal=Principal(kind="agent", id="test.agent"),
        tool="external_email.send",
        intent="Test action; must not execute without a human.",
        payload=payload or {"to": "compliance@northbridge.example",
                            "subject": "test", "body": "hello"},
        trace_id="test-trace",
        role="orchestrator",
    )


def test_prepared_action_starts_pending(system):
    action = _prepare(system)
    assert action.status is ActionStatus.PENDING
    assert action.payload_hash


def test_agent_cannot_approve(system):
    action = _prepare(system)
    with pytest.raises(AuthorizationError, match="human principal"):
        system.gateway.approve(
            action.action_id,
            Principal(kind="agent", id="orchestrator", token=system.settings.approver_token))
    assert system.gateway.get(action.action_id).status is ActionStatus.PENDING


def test_service_principal_cannot_approve(system):
    action = _prepare(system)
    with pytest.raises(AuthorizationError):
        system.gateway.approve(
            action.action_id,
            Principal(kind="service", id="cron", token=system.settings.approver_token))


def test_wrong_credential_is_rejected(system):
    action = _prepare(system)
    with pytest.raises(AuthorizationError, match="credential"):
        system.gateway.approve(action.action_id,
                               Principal(kind="human", id="attacker", token="guess"))


def test_deny_needs_the_same_credential_as_approve(system):
    action = _prepare(system)
    with pytest.raises(AuthorizationError, match="credential"):
        system.gateway.deny(action.action_id, Principal(kind="human", id="attacker"), "no")
    assert system.gateway.get(action.action_id).status is ActionStatus.PENDING


def test_execute_needs_the_same_credential_as_approve(system):
    action = _prepare(system)
    system.gateway.approve(action.action_id, _human(system))
    with pytest.raises(AuthorizationError, match="credential"):
        system.gateway.execute(action.action_id, Principal(kind="human", id="founder"))
    assert system.gateway.get(action.action_id).status is ActionStatus.APPROVED


def test_execution_without_approval_is_refused(system):
    action = _prepare(system)
    with pytest.raises(AuthorizationError, match="not APPROVED"):
        system.gateway.execute(action.action_id, _human(system))


def test_payload_tampering_after_approval_is_refused(system):
    action = _prepare(system)
    system.gateway.approve(action.action_id, _human(system))
    action.payload["to"] = "attacker@evil.invalid"
    with pytest.raises(AuthorizationError, match="payload changed"):
        system.gateway.execute(action.action_id, _human(system))
    assert system.gateway.get(action.action_id).status is ActionStatus.FAILED


def test_approved_action_executes_once_and_records_the_approver(system):
    action = _prepare(system)
    system.gateway.approve(action.action_id, _human(system))
    done = system.gateway.execute(action.action_id, _human(system))
    assert done.status is ActionStatus.EXECUTED
    assert done.approved_by == "founder"
    assert done.execution_result["simulated"] is True
    assert action.action_id in done.execution_result["message_id"]   # idempotency key on the wire


def test_retried_execute_does_not_send_twice(system):
    action = _prepare(system)
    system.gateway.approve(action.action_id, _human(system))
    first = system.gateway.execute(action.action_id, _human(system))
    second = system.gateway.execute(action.action_id, _human(system))
    assert second.execution_result == first.execution_result
    events = [e["event"] for e in system.audit.entries("test-trace")]
    assert "action_execute_replayed" in events


def test_crash_mid_send_does_not_resend_after_restart(make_settings):
    """The process dies inside the transport call; the restarted gateway must not send again."""
    from founder_agent.app import build_system
    s = make_settings()
    first = build_system(s)
    action = _prepare(first)
    first.gateway.approve(action.action_id, _human(first))

    class Crash(BaseException):
        pass

    from dataclasses import replace
    spec = first.tools.get("external_email.send")

    def dies_mid_send(payload, idempotency_key=""):
        raise Crash()

    first.tools.register(replace(spec, handler=dies_mid_send))
    with pytest.raises(Crash):
        first.gateway.execute(action.action_id, _human(first))

    restarted = build_system(s)                       # same stores, new process
    assert restarted.gateway.get(action.action_id).status is ActionStatus.EXECUTING
    with pytest.raises(AuthorizationError, match="never confirmed"):
        restarted.gateway.execute(action.action_id, _human(restarted))


def test_denied_action_cannot_execute(system):
    action = _prepare(system)
    system.gateway.deny(action.action_id, _human(system), "no")
    with pytest.raises(AuthorizationError):
        system.gateway.execute(action.action_id, _human(system))


def test_action_expires_when_the_clock_passes_its_ttl(system, tmp_path):
    now = [utcnow()]
    gateway = ActionGateway(system.tools, system.audit, system.settings,
                            store_path=tmp_path / "actions.jsonl", clock=lambda: now[0])
    action = _prepare(system, gateway=gateway)
    now[0] += timedelta(seconds=system.settings.action_ttl_seconds - 1)
    assert gateway.get(action.action_id).status is ActionStatus.PENDING
    now[0] += timedelta(seconds=2)
    assert gateway.get(action.action_id).status is ActionStatus.EXPIRED
    with pytest.raises(AuthorizationError, match="EXPIRED|not pending"):
        gateway.approve(action.action_id, _human(system))


def test_subordinate_agents_hold_no_consequential_capability(system):
    for role in ("research", "redteam", "technical"):
        allowed, reason = system.tools.authorize_call(role, "external_email.send")
        assert not allowed, f"{role} should not reach the email tool"
        assert "not granted" in reason


def test_orchestrator_ask_prepares_but_never_sends(system):
    response = system.orchestrator.ask(
        "Send Northbridge Bank the position of record", prepare_action=True)
    assert response.pending_action is not None
    assert response.pending_action.status is ActionStatus.PENDING


# -- content-based refusal (recommended modification 4.11) ------------------
def test_internal_claim_cannot_be_prepared_for_external_send(system):
    board = next(c for c in system.claims if c.claim_id == "DOC-10#c1")
    assert board.sensitivity == "internal"
    with pytest.raises(AuthorizationError, match="internal-only"):
        system.gateway.prepare(
            principal=Principal(kind="agent", id="t"), tool="external_email.send", intent="x",
            payload={"to": "a@b.example", "subject": "s", "body": "fyi"},
            trace_id="t", supporting_claim_ids=["DOC-10#c1"], role="orchestrator")


@pytest.mark.parametrize("claim_id", ["DOC-10#c1", "DOC-06#c3"])   # DOC-06#c3 has an em dash
def test_pasting_internal_text_without_citing_it_is_caught(system, claim_id):
    claim = next(c for c in system.claims if c.claim_id == claim_id)
    with pytest.raises(AuthorizationError, match=claim_id):
        system.gateway.prepare(
            principal=Principal(kind="agent", id="t"), tool="external_email.send", intent="x",
            payload={"to": "a@b.example", "subject": "s", "body": f"Note: {claim.text}"},
            trace_id="t", role="orchestrator")


def test_drafted_email_carries_only_cleared_material(system):
    response = system.orchestrator.ask(
        "Send Northbridge Bank our current custody position of record", prepare_action=True)
    by_id = {c.claim_id: c for c in system.claims}
    action = response.pending_action
    if action is None:
        assert any("internal-only" in c for c in response.answer.caveats)
        return
    assert all(by_id[c].sensitivity != "internal" for c in action.supporting_claim_ids)
    assert "DOC-10" not in action.payload["body"]
