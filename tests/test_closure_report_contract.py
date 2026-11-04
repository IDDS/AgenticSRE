"""The second-pass model receives the same evidence boundary in both routes."""
import pytest

from orchestrator.diagnosis_closure import ClosurePlan, DiagnosisClosureManager


@pytest.mark.parametrize('is_alert', [False, True])
def test_closure_prompt_preserves_identity_and_causal_boundaries(monkeypatch, is_alert):
    import agents.diagnosis_grounding as grounding

    class Model:
        def json_chat(self, messages, **kwargs):
            self.prompt = messages[1]['content']
            return {'root_cause': 'unresolved', 'root_entity': None}

    monkeypatch.setattr(grounding, 'alert_payload', lambda evidence: {'alert': True} if is_alert else None)
    monkeypatch.setattr(grounding, 'alert_report_prompt', lambda *args, **kwargs: 'alert-specific evidence')
    model = Model()
    DiagnosisClosureManager(model).revise_report('incident', {}, {}, {}, ClosurePlan())
    assert '不同对象的 UID 不同' in model.prompt
    assert '同时观测到' in model.prompt
    assert '缺乏证据' in model.prompt
    if not is_alert:
        assert '"kind":"Pod"' not in model.prompt
