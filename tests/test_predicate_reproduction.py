"""Run with pytest on snail against unchanged baseline."""
import json
from types import SimpleNamespace as NS
from agents.event_agent import EventAgent
from tools.base_tool import ToolResult
from orchestrator.diagnosis_closure import DiagnosisClosureManager,ClosurePlan

def test_full_pod_spec_is_not_dropped():
    pod={"metadata":{"name":"target","namespace":"shop","uid":"live-uid","resourceVersion":"11"},"spec":{"containers":[{"name":"app","resources":{}}],"volumes":[{"name":"api","projected":{}}]},"status":{"phase":"Pending"}}
    tool=NS(execute=lambda **kw:ToolResult(success=True,data={"items":[pod]}))
    a=EventAgent(None,NS(get=lambda name:tool))
    facts=a._build_current_state_evidence_items({},a._fetch_problem_pods("shop"))
    assert any(f["signal_type"]=="pod_spec_checks" for f in facts)

def test_closure_cannot_drop_a_current_uid_bound_original_report():
    original={"root_entity":{"kind":"Pod","name":"target","namespace":"shop","uid":"live-uid"},"root_cause":"target selector mismatch","fault_type":"scheduling_failure","confidence":.8}
    fact={"signal_type":"pod_waiting_state","source":"kubectl get pods -o json","observation":"Pending","raw_ref":{"kind":"Pod","name":"target","namespace":"shop","pod_uid":"live-uid","resource_version":"11"}}
    model=NS(json_chat=lambda *a,**k:{"root_cause":"target selector mismatch after follow-up","fault_type":"scheduling_failure","confidence":.8})
    result=DiagnosisClosureManager(model).revise_report("Pod/target Pending",original,{"event_agent":{"evidence_items":[fact]}},{},ClosurePlan())
    assert result.get("root_entity",{}).get("uid")=="live-uid"
