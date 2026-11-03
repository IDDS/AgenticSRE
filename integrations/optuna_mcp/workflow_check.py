"""Exercise the host workflow and fail-closed cleanup via the real MCP server."""
import asyncio

from .workflow import run_experiment
from .client import OptunaMCPClient, OptunaMCPError


async def main():
    calls = []
    async def measured(params, trial_id):
        calls.append(params)
        return {'value': float(params['threads']), 'evidence_ref': f'run-{trial_id}', 'cleanup_verified': True, 'correctness_verified': True}

    result = await run_experiment(objective='qps', direction='maximize', choices={'threads': [1, 2]}, executor=measured, max_trials=3, seed=42)
    assert result['status'] == 'completed' and len(calls) == 3
    assert result['summary']['completed'] == 3 and result['summary']['pending'] == 0

    calls.clear()
    async def unverified(params, trial_id):
        calls.append(params)
        return {'value': 999, 'evidence_ref': 'unverified', 'cleanup_verified': False}

    result = await run_experiment(objective='qps', direction='maximize', choices={'threads': [1, 2]}, executor=unverified, max_trials=3)
    assert result['status'] == 'stopped' and len(calls) == 1
    assert result['summary']['completed'] == 0 and result['summary']['failed'] == 1
    assert result['attempts'][0]['reason'] == 'cleanup_unverified'

    async def incorrect(params, trial_id):
        return {'value': 999, 'evidence_ref': 'incorrect', 'cleanup_verified': True, 'correctness_verified': False}

    result = await run_experiment(objective='qps', direction='maximize', choices={'threads': [1, 2]}, executor=incorrect, max_trials=2)
    assert result['summary']['completed'] == 0 and result['summary']['failed'] == 2 and result['summary']['best'] is None

    calls.clear()
    async def broken(params, trial_id):
        calls.append(params)
        raise RuntimeError('measurement failed')

    result = await run_experiment(objective='qps', direction='maximize', choices={'threads': [1, 2]}, executor=broken, max_trials=3)
    assert result['status'] == 'stopped' and len(calls) == 1 and result['summary']['failed'] == 1
    class SlowSession:
        calls = 0
        async def call_tool(self, name, arguments):
            self.calls += 1
            await asyncio.sleep(.1)
    slow = SlowSession()
    client = OptunaMCPClient(slow, timeout_seconds=.001)
    for _ in range(2):
        try:
            await client.call('suggest_trial', {'study_id': 'x'})
        except OptunaMCPError:
            pass
        else:
            raise AssertionError('timed out session accepted a call')
    assert slow.calls == 1
    print('MCP host workflow check: PASS; measured trials and unverified cleanup fail-closed')


if __name__ == '__main__':
    asyncio.run(main())
