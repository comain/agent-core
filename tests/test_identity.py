"""Tests for identity, the deployment guard, and gate attribution.

The guard tests matter most. Header trust is safe only when the service is
unreachable except through the proxy, and the failure mode when that stops
being true is silent and total: anyone on the network can act as anyone.
"""

from __future__ import annotations

import pytest

from agent_core.identity import (
    ANSWER_GATE,
    MUTATE_TASK,
    READ_REPORT,
    AuthenticationError,
    AuthorizationError,
    IdentityResolver,
    InsecureDeploymentError,
    Policy,
    Principal,
    ProxyHeaderResolver,
    ServiceTokenResolver,
    assert_trusted_deployment,
)
from agent_core.runtime import GateAlreadyAnswered, RuntimeStore


# -- Principal -----------------------------------------------------------------


def test_principal_requires_a_subject():
    with pytest.raises(ValueError):
        Principal(subject="", kind="user")
    with pytest.raises(ValueError):
        Principal(subject="   ", kind="user")


def test_principal_kind_is_constrained():
    with pytest.raises(ValueError):
        Principal(subject="x", kind="robot")


def test_principal_repr_does_not_dump_groups():
    p = Principal(subject="alice", kind="user", groups=frozenset({"secret-team"}))
    assert "secret-team" not in repr(p)
    assert repr(p) == "Principal(user:alice)"


# -- deployment guard ----------------------------------------------------------


def test_guard_allows_loopback_binding():
    for host in ("127.0.0.1", "::1", "localhost"):
        assert_trusted_deployment(bind_host=host, trust_proxy_headers=True, proxy_is_fronting=False)


def test_guard_refuses_public_bind_without_a_declared_proxy():
    """The configuration that silently lets anyone impersonate anyone."""
    with pytest.raises(InsecureDeploymentError) as exc:
        assert_trusted_deployment(bind_host="0.0.0.0", trust_proxy_headers=True, proxy_is_fronting=False)
    assert "impersonate" in str(exc.value)


def test_guard_allows_public_bind_when_a_proxy_is_declared():
    assert_trusted_deployment(bind_host="0.0.0.0", trust_proxy_headers=True, proxy_is_fronting=True)


def test_guard_is_irrelevant_when_header_trust_is_off():
    assert_trusted_deployment(bind_host="0.0.0.0", trust_proxy_headers=False, proxy_is_fronting=False)


def test_guard_escape_hatch_is_loud(caplog):
    import logging
    with caplog.at_level(logging.ERROR):
        assert_trusted_deployment(
            bind_host="0.0.0.0", trust_proxy_headers=True, proxy_is_fronting=False, allow_insecure=True
        )
    assert any("INSECURE" in r.message for r in caplog.records)


def test_trusting_headers_is_not_by_itself_a_topology_claim():
    """Enabling auth must not silently also assert production ingress."""
    with pytest.raises(InsecureDeploymentError):
        assert_trusted_deployment(bind_host="10.0.0.5", trust_proxy_headers=True, proxy_is_fronting=False)


# -- proxy header resolution ---------------------------------------------------


def test_proxy_resolver_reads_identity():
    r = ProxyHeaderResolver()
    p = r.resolve({
        "X-Forwarded-User": "alice",
        "X-Forwarded-Email": "alice@example.com",
        "X-Forwarded-Groups": "eng, reviewers ,",
    })
    assert p.subject == "alice" and p.kind == "user"
    assert p.email == "alice@example.com"
    assert p.groups == frozenset({"eng", "reviewers"})


def test_proxy_resolver_is_header_case_insensitive():
    assert ProxyHeaderResolver().resolve({"x-forwarded-user": "bob"}).subject == "bob"


def test_proxy_resolver_returns_none_when_absent():
    assert ProxyHeaderResolver().resolve({}) is None
    assert ProxyHeaderResolver().resolve({"X-Forwarded-User": "  "}) is None


def test_proxy_header_name_is_configurable():
    r = ProxyHeaderResolver(user_header="Remote-User")
    assert r.resolve({"Remote-User": "carol"}).subject == "carol"


# -- service tokens ------------------------------------------------------------


def test_service_token_authenticates_a_machine():
    r = ServiceTokenResolver({"rdc": "s3cret"})
    p = r.resolve({"Authorization": "Bearer s3cret"})
    assert p.subject == "rdc" and p.kind == "service" and not p.is_human


def test_service_token_accepts_bare_token():
    assert ServiceTokenResolver({"rdc": "s3cret"}).resolve({"Authorization": "s3cret"}).subject == "rdc"


def test_wrong_service_token_is_an_error_not_anonymous():
    """Absent and invalid are different: invalid always means a broken caller."""
    with pytest.raises(AuthenticationError):
        ServiceTokenResolver({"rdc": "s3cret"}).resolve({"Authorization": "Bearer nope"})


def test_no_token_is_anonymous():
    assert ServiceTokenResolver({"rdc": "s3cret"}).resolve({}) is None


def test_empty_configured_token_never_matches():
    """A blank token in config must not turn into a skeleton key."""
    with pytest.raises(AuthenticationError):
        ServiceTokenResolver({"rdc": ""}).resolve({"Authorization": "Bearer "})
    assert ServiceTokenResolver({"rdc": ""}).resolve({"Authorization": ""}) is None


# -- combined resolver ---------------------------------------------------------


def test_presenting_both_mechanisms_is_refused():
    r = IdentityResolver(service=ServiceTokenResolver({"rdc": "tok"}))
    with pytest.raises(AuthenticationError) as exc:
        r.resolve({"X-Forwarded-User": "alice", "Authorization": "Bearer tok"})
    assert "refusing to guess" in str(exc.value)


def test_proxy_headers_ignored_when_trust_disabled():
    r = IdentityResolver(trust_proxy_headers=False)
    assert r.resolve({"X-Forwarded-User": "alice"}) is None


# -- policy --------------------------------------------------------------------


def test_protected_action_requires_a_principal():
    with pytest.raises(AuthenticationError):
        Policy().authorize(None, ANSWER_GATE)
    with pytest.raises(AuthenticationError):
        Policy().authorize(None, MUTATE_TASK)


def test_open_action_tolerates_anonymous():
    """Staged enforcement: report reads stay open for now."""
    assert Policy().authorize(None, READ_REPORT) is None


def test_service_principal_cannot_answer_a_human_gate_by_default():
    """A pipeline approving its own gate defeats the gate."""
    svc = Principal(subject="rdc", kind="service")
    with pytest.raises(AuthorizationError):
        Policy().authorize(svc, ANSWER_GATE)


def test_service_gate_answers_allowed_when_configured():
    svc = Principal(subject="rdc", kind="service")
    assert Policy(allow_service_gate_answers=True).authorize(svc, ANSWER_GATE) is svc


def test_service_may_still_mutate_tasks():
    svc = Principal(subject="rdc", kind="service")
    assert Policy().authorize(svc, MUTATE_TASK) is svc


def test_required_groups_enforced_when_configured():
    policy = Policy(required_groups=frozenset({"reviewers"}))
    outsider = Principal(subject="dave", kind="user", groups=frozenset({"eng"}))
    insider = Principal(subject="erin", kind="user", groups=frozenset({"eng", "reviewers"}))
    with pytest.raises(AuthorizationError):
        policy.authorize(outsider, ANSWER_GATE)
    assert policy.authorize(insider, ANSWER_GATE) is insider


def test_any_authenticated_user_allowed_by_default():
    p = Principal(subject="frank", kind="user")
    assert Policy().authorize(p, ANSWER_GATE) is p


def test_allow_anonymous_lets_an_unauthenticated_caller_mutate_a_task():
    """The staging switch for adopting the shared router in an open service.

    Enforcing on day one would 401 every existing caller, so a product can
    mount the router first and tighten later in one place.
    """
    actor = Policy(allow_anonymous=True).authorize(None, MUTATE_TASK)
    assert actor.subject == "anonymous"
    assert actor.kind == "service"


def test_the_anonymous_subject_is_configurable_so_the_audit_trail_names_the_service():
    actor = Policy(allow_anonymous=True, anonymous_subject="cr-open").authorize(None, MUTATE_TASK)
    assert actor.subject == "cr-open"


def test_allow_anonymous_gates_mints_a_human_principal():
    actor = Policy(allow_anonymous_gates=True).authorize(None, ANSWER_GATE)
    assert actor.kind == "user"
    assert actor.subject == "anonymous"


def test_allow_anonymous_gates_does_not_let_service_tokens_answer():
    svc = Principal(subject="rdc", kind="service")
    with pytest.raises(AuthorizationError):
        Policy(allow_anonymous_gates=True).authorize(svc, ANSWER_GATE)


def test_allow_anonymous_does_not_open_human_gates():
    """Opening task control must not also let anyone approve a gate.

    The anonymous principal is a service principal and falls through the same
    check a real pipeline does, rather than returning early.
    """
    with pytest.raises(AuthorizationError):
        Policy(allow_anonymous=True).authorize(None, ANSWER_GATE)


def test_allow_anonymous_still_honours_required_groups():
    """The staging flag must not be a way around a configured restriction."""
    with pytest.raises(AuthorizationError):
        Policy(allow_anonymous=True, required_groups=frozenset({"sre"})).authorize(None, MUTATE_TASK)


# -- gate attribution ----------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = RuntimeStore(tmp_path / "rt.db")
    s.init()
    return s


def test_gate_answer_attributed_to_principal(store):
    gate = store.open_gate(task_ref="t", node="design_review", kind="input", prompt={})
    p = Principal(subject="alice", kind="user")
    answered = store.answer_gate(gate_id=gate.gate_id, response={"decision": "approve"}, principal=p)
    assert answered.answered_by == "alice"


def test_gate_answer_rejects_unauthorized_principal(store):
    gate = store.open_gate(task_ref="t", node="n", kind="approve", prompt={})
    svc = Principal(subject="rdc", kind="service")
    with pytest.raises(AuthorizationError):
        store.answer_gate(gate_id=gate.gate_id, response=True, principal=svc)
    assert store.get_gate(gate.gate_id).state == "pending"  # unchanged


def test_cannot_claim_one_identity_and_record_another(store):
    """Accepting both would let a caller launder attribution."""
    gate = store.open_gate(task_ref="t", node="n", kind="approve", prompt={})
    p = Principal(subject="alice", kind="user")
    with pytest.raises(ValueError):
        store.answer_gate(gate_id=gate.gate_id, response=True, principal=p, answered_by="someone-else")


def test_legacy_answered_by_still_works(store):
    """Products not yet on identity must keep functioning during migration."""
    gate = store.open_gate(task_ref="t", node="n", kind="approve", prompt={})
    assert store.answer_gate(gate_id=gate.gate_id, response=True, answered_by="legacy").answered_by == "legacy"
