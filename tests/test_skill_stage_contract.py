import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skill_runtime.stage_contract import evaluate_skill_stage_contract


def test_all_core_skills_have_consistent_stage_contracts() -> None:
    failures = {}
    skill_dirs = sorted(path.parent for path in (ROOT / "skill_runtime" / "skills").glob("*/skill.yaml"))

    for skill_dir in skill_dirs:
        contract = evaluate_skill_stage_contract(skill_dir)
        if not contract.valid:
            failures[skill_dir.name] = contract.errors

    assert not failures, failures


def test_single_stage_skill_documents_only_its_declared_stage() -> None:
    skill_dir = ROOT / "skill_runtime" / "skills" / "generic_fault_detection"
    contract = evaluate_skill_stage_contract(skill_dir)

    assert contract.declared_stages == ["detect"]
    assert contract.documented_stages == ["detect"]
    assert contract.adapter_stages == ["detect"]
