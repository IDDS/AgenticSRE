import asyncio
from types import SimpleNamespace

from agents.metric_agent import MetricAgent
from tools.base_tool import ToolResult


def test_structured_only_mode_preserves_measurements_without_requesting_prose():
    class Model:
        calls = 0

        async def async_chat(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError('optional prose was not requested')

    model = Model()
    tool = SimpleNamespace(execute=lambda **kwargs: ToolResult(success=True, data={'results': [
        {'metric': {'namespace': 'shop', 'pod': 'api'}, 'values': [[100, '1'], [130, '2']]}]}))
    agent = MetricAgent(model, SimpleNamespace(get=lambda _: tool))
    agent.summarize = False
    result = asyncio.run(agent.analyze('Pod/api Pending', 'shop', ['application']))
    assert model.calls == 0 and result['summary_status'] == 'not_requested'
    assert result['collection_status'] == 'ok' and result['error'] is False
    assert result['evidence_items'][0]['raw_ref']['samples'] == [[100., 1.], [130., 2.]]


def test_structured_only_mode_does_not_mask_collection_failure():
    tool = SimpleNamespace(execute=lambda **kwargs: ToolResult(success=False, error='unavailable'))
    agent = MetricAgent(None, SimpleNamespace(get=lambda _: tool))
    agent.summarize = False
    result = asyncio.run(agent.analyze('Pod/api Pending', 'shop', ['application']))
    assert result['error'] is True and not result['evidence_items']
    assert result['collection_errors']
