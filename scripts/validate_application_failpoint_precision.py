#!/usr/bin/env python3
"""Validate the fixed local application-failpoint execution profile."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "deploy" / "precision-fault-injection" / "application-failpoint" / "probe.py"
PROFILE = "agenticsre.local.application_failpoint.v1"
sys.dont_write_bytecode = True


def main() -> int:
    module_spec = importlib.util.spec_from_file_location("agenticsre_failpoint_probe", ARTIFACT)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("unable to load reviewed failpoint artifact")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)

    now = [100.0]
    registry = module.FailpointRegistry(clock=lambda: now[0])
    target_request = "agenticsre-fp-request"
    control_request = "agenticsre-control-request"
    before = module.checkout_after_capture(registry, target_request)
    registry.enable(module.Activation(
        token="agenticsre-local-fp-token",
        failpoint_id="checkout.payment.after_capture",
        request_id=target_request,
        error_code="PAYMENT_STATE_WRITE_FAILED",
        remaining_hits=1,
        expires_at_monotonic=115.0,
    ))
    fault = module.checkout_after_capture(registry, target_request)
    control = module.checkout_after_capture(registry, control_request)
    exhausted = module.checkout_after_capture(registry, target_request)
    registry.disable("agenticsre-local-fp-token")
    recovered = module.checkout_after_capture(registry, target_request)
    registry.enable(module.Activation(
        token="agenticsre-local-fp-token",
        failpoint_id="checkout.payment.after_capture",
        request_id=target_request,
        error_code="PAYMENT_STATE_WRITE_FAILED",
        remaining_hits=1,
        expires_at_monotonic=115.0,
    ))
    now[0] = 116.0
    expired = module.checkout_after_capture(registry, target_request)
    registry.disable("agenticsre-local-fp-token")
    if not (
        before == "ok"
        and fault == "error:PAYMENT_STATE_WRITE_FAILED"
        and control == "ok"
        and exhausted == "ok"
        and recovered == "ok"
        and expired == "ok"
    ):
        raise RuntimeError("application failpoint precision validation failed")

    print(f"executor_profile={PROFILE}")
    print(f"artifact_sha256={hashlib.sha256(ARTIFACT.read_bytes()).hexdigest()}")
    print(f"before_evidence=request:{target_request},outcome:{before}")
    print(f"fault_evidence=request:{target_request},outcome:{fault},max_hits:1")
    print(f"during_evidence=control:{control_request}:{control},exhausted_target:{exhausted}")
    print(f"recovery_evidence=token:disabled,target:{recovered},expired_target:{expired}")
    print("validation=PASS application-failpoint exact_match=true bounded_hit=true expiry=true control=true recovery=true cleanup=complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
