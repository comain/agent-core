from agent_core.model_selection.policy import Candidate, ModelPolicy, resolve_selection


def test_higher_effort_keeps_model_and_excludes_max():
    variants = [Candidate(identity='pool/model', effort=e, variant=e, score=80.0,
                          benchmark_id=e, capability_approved=True)
                for e in ['low', 'medium', 'high', 'max']]
    policy = ModelPolicy(application_id='test')
    selected = resolve_selection(variants, policy, effort_strategy='higher')
    assert selected.candidates[0].effort == 'medium'
    assert resolve_selection(variants, policy).candidates[0].effort == 'low'


def test_higher_effort_obeys_admission_and_retains_current_without_upgrade():
    variants = [Candidate(identity='pool/model', effort=e, variant=e, score=80.0,
                          benchmark_id=e, capability_approved=True) for e in ['low', 'high', 'max']]
    selected = resolve_selection(variants, ModelPolicy(application_id='test'),
                                 effort_strategy='higher',
                                 admission_check=lambda c: 'unavailable' if c.effort == 'high' else None)
    assert selected.candidates[0].effort == 'low'


def test_higher_effort_does_not_upgrade_to_unsuffixed_default():
    variants = [Candidate(identity='pool/model', effort=e, variant=e or 'default', score=80.0,
                          benchmark_id=e or 'default', capability_approved=True)
                for e in ['low', '']]
    selected = resolve_selection(variants, ModelPolicy(application_id='test'),
                                 effort_strategy='higher')
    assert selected.candidates[0].effort == 'low'
