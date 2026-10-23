"""Opt-in harness child: verify actual Pod logs despite later health access lines."""
import argparse
import json
from pathlib import Path

from agents.log_agent import LogAgent, select_log_samples
from tools import build_tool_registry
from web_app.app import _apply_platform_config, _cluster_scoped_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    root = parser.parse_args().evidence
    observation = json.loads((root/'log-sampling-observation.json').read_text())
    while observation.get('dependency'):
        observation = observation['dependency']
    ns = json.loads((root/'inventory.json').read_text())['pods'][0]['namespace']
    _apply_platform_config()
    agent = LogAgent(None, build_tool_registry(_cluster_scoped_config('default'), isolated=True))
    logs = agent._fetch_pod_logs(observation['pod'], ns)
    assert logs and all('"trace_id"' not in line for line in logs['lines'][-2:])
    selected = select_log_samples(logs['lines'], observation['pod'])
    text = '\n'.join(item['text'] for item in selected)[:1500]
    rows = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    change_id = observation['state']['change_id']
    gates = dict(
        transition_retained=any(r.get('event') == 'state_transition' and r.get('after') is True
                                and r.get('change_id') == change_id for r in rows),
        request_retained=any(r.get('trace_id') == observation['trace_id'] and r.get('code') == 503
                            and r.get('state', {}).get('change_id') == change_id for r in rows),
    )
    report = dict(passed=all(gates.values()), gates=gates, command=logs['command'],
                  fetched_lines=logs['count'], selected=selected,
                  scope='real HTTP and kubectl logs, health-tail interference; no LLM/RCA acceptance')
    (root/'log-sampling-acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'passed': report['passed'], 'gates': gates}))
    assert report['passed']


if __name__ == '__main__':
    main()
