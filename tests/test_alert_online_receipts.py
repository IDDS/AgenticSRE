"""The acceptance recorder must preserve actual sync/async client interfaces."""
import asyncio
import json

import pytest

from eval.alert_online_app import install_receipts
from tools.llm_client import LLMClient
from tools.base_tool import SRETool
from orchestrator.pipeline import Pipeline


@pytest.mark.parametrize('count',[2,17])
def test_real_async_signature_budget_and_provider_receipts(monkeypatch,tmp_path,count):
    received=[]
    def provider(self,messages,temperature=None,max_tokens=None,model=None,**kwargs):
        received.append((temperature,max_tokens,model))
        kwargs['_receipt'].update(completion_status='complete',usage={'prompt_tokens':5,'completion_tokens':2,'total_tokens':7})
        return 'answer'
    async def pipeline(self,*args,**kwargs):
        client=object.__new__(LLMClient)
        return [await client.async_chat([{'role':'user','content':'observe'}],.7,9000,'fixture') for _ in range(count)]
    # Register every overwritten class method with pytest for teardown.
    monkeypatch.setattr(LLMClient,'chat',provider)
    monkeypatch.setattr(Pipeline,'run',pipeline)
    monkeypatch.setattr(SRETool,'execute',SRETool.execute)
    install_receipts(tmp_path)
    client=object.__new__(LLMClient)
    with pytest.raises(RuntimeError,match='outside an active'):
        client.chat([])
    if count>16:
        with pytest.raises(TimeoutError,match='budget exhausted'):
            asyncio.run(object.__new__(Pipeline).run('query','ns'))
    else:
        assert asyncio.run(object.__new__(Pipeline).run('query','ns'))==['answer']*count
    rows=json.loads((tmp_path/'calls.json').read_text())
    assert len(rows)==min(count,16)==len(received)
    assert all(row['provider']['usage']['total_tokens']==7 for row in rows)
    assert received==[(0,2048,'fixture')]*len(received)
    assert json.loads((tmp_path/'runs.json').read_text())[0]['finished_at']
