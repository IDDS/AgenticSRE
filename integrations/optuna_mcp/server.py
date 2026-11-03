"""Opt-in MCP server for bounded, in-memory configuration search.

The server only suggests configurations and records caller-supplied observations.
It does not execute workloads, access external resources, or apply configuration.
Studies are process-local and disappear when the server exits.
"""
from __future__ import annotations

import math
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import optuna
from mcp.server.fastmcp import FastMCP

optuna.logging.set_verbosity(optuna.logging.WARNING)
mcp = FastMCP('agenticsre-optuna')
_LOCK = threading.RLock()
_MAX_STUDIES = 16
_MAX_TRIALS = 64
_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,47}$')


@dataclass
class _Study:
    study: Any
    choices: dict[str, list[int | float | str | bool]]
    objective: str
    pending: dict[int, Any] = field(default_factory=dict)
    evidence: dict[int, str] = field(default_factory=dict)


_STUDIES: dict[str, _Study] = {}


def _validate_choices(choices: dict[str, list[Any]]) -> dict[str, list[Any]]:
    if not isinstance(choices, dict) or not 1 <= len(choices) <= 8:
        raise ValueError('choices must contain 1 to 8 parameters')
    result = {}
    for name, options in choices.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError('invalid parameter name')
        if not isinstance(options, list) or not 2 <= len(options) <= 16:
            raise ValueError('each parameter needs 2 to 16 choices')
        for value in options:
            if type(value) not in (str, int, float, bool):
                raise ValueError('unsupported choice type')
            if isinstance(value, str) and (not value or len(value) > 128):
                raise ValueError('invalid string choice')
            if type(value) in (int, float) and (not math.isfinite(value) or abs(value) > 1e12):
                raise ValueError('invalid numeric choice')
        if len({(type(x).__name__, str(x)) for x in options}) != len(options):
            raise ValueError('duplicate choices')
        # Python/Optuna consider e.g. True and 1 equal; avoid ambiguous round trips.
        if len({str(x).casefold() for x in options}) != len(options) or any(a == b for i, a in enumerate(options) for b in options[i + 1:]):
            raise ValueError('ambiguous choices')
        result[name] = list(options)
    return result


@mcp.tool()
def create_study(objective: str, direction: str, choices: dict[str, list[Any]], seed: int = 0) -> dict[str, Any]:
    """Create an in-memory bounded study; choices are a caller-approved search space."""
    if not isinstance(objective, str) or not _NAME.fullmatch(objective):
        raise ValueError('invalid objective name')
    if direction not in ('minimize', 'maximize'):
        raise ValueError('invalid direction')
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError('invalid seed')
    validated = _validate_choices(choices)
    with _LOCK:
        if len(_STUDIES) >= _MAX_STUDIES:
            raise ValueError('study capacity reached')
        identifier = uuid.uuid4().hex
        study = optuna.create_study(direction=direction, sampler=optuna.samplers.TPESampler(seed=seed))
        _STUDIES[identifier] = _Study(study, validated, objective)
    return {'study_id': identifier, 'objective': objective, 'direction': direction, 'status': 'created', 'persistent': False}


@mcp.tool()
def suggest_trial(study_id: str) -> dict[str, Any]:
    """Suggest a trial; no configuration is applied and no workload is run."""
    with _LOCK:
        entry = _STUDIES.get(study_id)
        if entry is None:
            raise ValueError('study not found')
        if len(entry.study.trials) >= _MAX_TRIALS:
            raise ValueError('trial capacity reached')
        trial = entry.study.ask()
        try:
            params = {name: trial.suggest_categorical(name, choices) for name, choices in entry.choices.items()}
        except Exception:
            entry.study.tell(trial, state=optuna.trial.TrialState.FAIL)
            raise
        entry.pending[trial.number] = trial
        return {'study_id': study_id, 'trial_id': trial.number, 'parameters': params, 'status': 'suggested', 'applied': False}


@mcp.tool()
def report_trial(study_id: str, trial_id: int, status: str, value: float | None = None, evidence_ref: str = '') -> dict[str, Any]:
    """Record one caller-measured objective or failure; evidence is not verified here."""
    if type(trial_id) is not int or trial_id < 0:
        raise ValueError('invalid trial id')
    if status not in ('complete', 'failed'):
        raise ValueError('invalid status')
    if not isinstance(evidence_ref, str) or len(evidence_ref) > 512:
        raise ValueError('invalid evidence reference')
    if status == 'complete' and (type(value) not in (int, float) or not math.isfinite(value)):
        raise ValueError('complete trials need a finite numeric value')
    if status == 'failed' and value is not None:
        raise ValueError('failed trial cannot have a value')
    with _LOCK:
        entry = _STUDIES.get(study_id)
        if entry is None or trial_id not in entry.pending:
            raise ValueError('study or pending trial not found')
        trial = entry.pending[trial_id]
        if status == 'complete':
            entry.study.tell(trial, float(value))
        else:
            entry.study.tell(trial, state=optuna.trial.TrialState.FAIL)
        del entry.pending[trial_id]
        entry.evidence[trial_id] = evidence_ref
        return {'study_id': study_id, 'trial_id': trial_id, 'status': status, 'evidence_verified': False}


@mcp.tool()
def study_summary(study_id: str) -> dict[str, Any]:
    """Return completed, failed and pending counts and the best measured candidate."""
    with _LOCK:
        entry = _STUDIES.get(study_id)
        if entry is None:
            raise ValueError('study not found')
        trials = entry.study.trials
        complete = [t for t in trials if t.state == optuna.trial.TrialState.COMPLETE]
        failed = [t for t in trials if t.state == optuna.trial.TrialState.FAIL]
        best = entry.study.best_trial if complete else None
        return {'study_id': study_id, 'objective': entry.objective, 'direction': entry.study.direction.name.lower(),
                'completed': len(complete), 'failed': len(failed), 'pending': len(entry.pending),
                'best': None if best is None else {'trial_id': best.number, 'parameters': best.params, 'value': best.value,
                                                  'evidence_ref': entry.evidence.get(best.number, '')},
                'evidence_verified': False, 'persistent': False}


@mcp.tool()
def close_study(study_id: str) -> dict[str, Any]:
    """Discard a completed in-memory study; pending trials must be resolved first."""
    with _LOCK:
        entry = _STUDIES.get(study_id)
        if entry is None:
            raise ValueError('study not found')
        if entry.pending:
            raise ValueError('pending trials must be reported before closing')
        del _STUDIES[study_id]
        return {'study_id': study_id, 'status': 'closed'}


if __name__ == '__main__':
    mcp.run(transport='stdio')
