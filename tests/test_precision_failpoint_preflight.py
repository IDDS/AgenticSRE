import hashlib

import pytest

from skill_runtime.precision_execution import PrecisionExecutionError, precision_adapter_preflight


SCENARIO = "precise-application-failpoint-error"
RUNNER = "scripts/validate_application_failpoint_precision.py"
ASSET = "deploy/precision-fault-injection/application-failpoint/probe.py"


def test_failpoint_preflight_checks_probe_and_hash(tmp_path):
    runner = tmp_path / RUNNER
    runner.parent.mkdir(parents=True)
    runner.write_text("runner")

    blocked = precision_adapter_preflight(SCENARIO, root_dir=tmp_path)
    assert not blocked["ready"]
    assert any(ASSET in reason for reason in blocked["blockers"])

    probe = tmp_path / ASSET
    probe.parent.mkdir(parents=True)
    probe.write_text("probe")
    ready = precision_adapter_preflight(SCENARIO, root_dir=tmp_path)
    assert ready["ready"]
    assert ready["reviewed_dependencies"] == [{
        "path": ASSET, "sha256": hashlib.sha256(b"probe").hexdigest(),
    }]


def test_failpoint_preflight_rejects_dependency_outside_root(tmp_path):
    root = tmp_path / "root"
    runner = root / RUNNER
    runner.parent.mkdir(parents=True)
    runner.write_text("runner")
    outside = tmp_path / "outside"
    outside.write_text("probe")
    probe = root / ASSET
    probe.parent.mkdir(parents=True)
    probe.symlink_to(outside)

    with pytest.raises(PrecisionExecutionError, match="路径越界"):
        precision_adapter_preflight(SCENARIO, root_dir=root)
