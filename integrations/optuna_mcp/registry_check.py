"""Exercise opt-in ToolRegistry integration against the real MCP server."""
import asyncio

from tools import build_tool_registry


async def measure(params, trial_id):
    return {'value': float(params['threads']), 'evidence_ref': f'trial-{trial_id}',
            'correctness_verified': True, 'cleanup_verified': True}


async def incorrect(params, trial_id):
    return {'value': 999.0, 'evidence_ref': 'incorrect', 'correctness_verified': False, 'cleanup_verified': True}


def main():
    policy = {'objective': 'qps', 'direction': 'maximize', 'choices': {'threads': [1, 2]}, 'max_trials': 2, 'seed': 1}
    default = build_tool_registry(isolated=True)
    assert default.get('optuna_experiment') is None
    try:
        build_tool_registry(isolated=True, optuna_policy=policy)
    except ValueError:
        pass
    else:
        raise AssertionError('missing trusted executor accepted')
    registry = build_tool_registry(isolated=True, optuna_policy=policy, optuna_executor=measure)
    policy['choices']['threads'][0] = 999
    rejected = registry.execute('optuna_experiment', command='arbitrary')
    assert not rejected.success
    result = registry.execute('optuna_experiment')
    assert result.success and result.data['summary']['completed'] == 2
    assert all(a['parameters']['threads'] in (1, 2) for a in result.data['attempts'])
    failed = build_tool_registry(isolated=True, optuna_policy={'objective': 'qps', 'direction': 'maximize', 'choices': {'threads': [1, 2]}, 'max_trials': 1}, optuna_executor=incorrect)
    assert not failed.execute('optuna_experiment').success

    async def check_async():
        tool = registry.get('optuna_experiment')
        assert not registry.execute('optuna_experiment').success
        async_result = await tool.run_async()
        assert async_result.success
    asyncio.run(check_async())
    print('Optuna ToolRegistry check: PASS; opt-in policy, actual MCP, rejection and failure semantics')


if __name__ == '__main__':
    main()
