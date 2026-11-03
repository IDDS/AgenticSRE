"""Opt-in orchestration for a caller-owned, scoped experiment executor."""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Any

from .client import connect_optuna

# The executor is trusted application code, never a command supplied by the model.
Executor = Callable[[dict[str, Any], int], Awaitable[dict[str, Any]]]


async def run_experiment(*, objective: str, direction: str, choices: dict[str, list[Any]],
                         executor: Executor, max_trials: int = 4, seed: int = 0) -> dict[str, Any]:
    """Run a bounded experiment. Stop immediately when rollback cannot be verified.

    The executor is responsible for scope, authorization, timeout, actual measurement,
    correctness checks, and rollback. It must return cleanup_verified, correctness_verified, value and an
    evidence_ref. Missing/invalid evidence or cleanup is a failed trial, never a score.
    """
    if type(max_trials) is not int or not 1 <= max_trials <= 16:
        raise ValueError('max_trials must be between 1 and 16')
    if not callable(executor):
        raise ValueError('trusted executor is required')
    attempts: list[dict[str, Any]] = []
    async with connect_optuna() as client:
        study = await client.call('create_study', {'objective': objective, 'direction': direction, 'choices': choices, 'seed': seed})
        sid = study['study_id']
        stopped = False
        for _ in range(max_trials):
            suggestion = await client.call('suggest_trial', {'study_id': sid})
            number = suggestion['trial_id']
            attempt: dict[str, Any] = {'trial_id': number, 'parameters': suggestion['parameters']}
            attempts.append(attempt)
            parameters = suggestion['parameters']
            allowed = (isinstance(parameters, dict) and set(parameters) == set(choices)
                       and all(any(type(v) is type(option) and v == option for option in choices[k])
                               for k, v in parameters.items()))
            if not allowed:
                attempt.update(status='failed', reason='suggestion_outside_approved_choices')
                await client.call('report_trial', {'study_id': sid, 'trial_id': number, 'status': 'failed'})
                stopped = True
                break
            try:
                observation = await executor(dict(suggestion['parameters']), number)
            except Exception as exc:
                attempt.update(status='failed', reason='executor_error', error_type=type(exc).__name__)
                await client.call('report_trial', {'study_id': sid, 'trial_id': number, 'status': 'failed'})
                stopped = True
                break
            if not isinstance(observation, dict):
                observation = {}
            cleanup_verified = observation.get('cleanup_verified') is True
            correctness_verified = observation.get('correctness_verified') is True
            evidence = observation.get('evidence_ref')
            value = observation.get('value')
            try:
                numeric = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                numeric = False
            valid = cleanup_verified and correctness_verified and isinstance(evidence, str) and 0 < len(evidence) <= 512 and numeric
            if not valid:
                reason = 'cleanup_unverified' if not cleanup_verified else 'invalid_measurement_or_evidence'
                attempt.update(status='failed', reason=reason)
                await client.call('report_trial', {'study_id': sid, 'trial_id': number, 'status': 'failed'})
                if not cleanup_verified:
                    stopped = True
                    break
                continue
            attempt.update(status='complete', value=value, evidence_ref=evidence, cleanup_verified=True, correctness_verified=True)
            await client.call('report_trial', {'study_id': sid, 'trial_id': number, 'status': 'complete', 'value': value, 'evidence_ref': evidence})
        summary = await client.call('study_summary', {'study_id': sid})
        await client.call('close_study', {'study_id': sid})
    return {'status': 'stopped' if stopped else 'completed', 'attempts': attempts, 'summary': summary,
            'limitations': ['executor evidence and cleanup claims require independent verification', 'not an acceptance result']}
