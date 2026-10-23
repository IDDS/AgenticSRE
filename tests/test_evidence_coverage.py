from agents.evidence_coverage import assess_evidence_coverage, extract_literal_facts


def test_extract_literal_facts_is_generic_and_keeps_direct_values():
    facts = extract_literal_facts(
        "Fault: call=4 injected=1 result=-1 errno=5. "
        "Recovery: cpu.max=100000 100000, latency=10ms."
    )
    assert "call=4" in facts
    assert "injected=1" in facts
    assert "errno=5" in facts
    assert any("cpu.max=100000 100000" in item for item in facts)
    assert any("10ms" in item for item in facts)


def test_coverage_gate_flags_dropped_literal_observations_without_truth_labels():
    audit = assess_evidence_coverage(
        "A function call returns an error and recovers.",
        "Fault: call=4 injected=1 result=-1 errno=5. Recovery: result=10 errno=0.",
        {"root_cause": "a transient error occurred and then recovered"},
        minimum_coverage=0.75,
    )
    assert audit["needs_rewrite"] is True
    assert audit["missing_facts"]
