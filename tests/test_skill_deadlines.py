"""Execute on the remote host: deadlines must stop work, not just waiting."""
import os
import time
from pathlib import Path

from skill_runtime.executor import SkillExecutor
from skill_runtime.registry import SkillRegistry


def make_executor(tmp_path, monkeypatch, source, timeout='.3'):
    monkeypatch.setenv('AGENTICSRE_SKILL_STAGE_TIMEOUT_S', timeout)
    skill=tmp_path/'sample'
    skill.mkdir()
    (skill/'skill.yaml').write_text('id: test.deadline\nversion: 1.0\nname: deadline\ndomain: test\nstages: [detect, diagnose, heal, verify]\n')
    (skill/'skill.py').write_text(source)
    return SkillExecutor(SkillRegistry(tmp_path).load())


def test_stage_timeout_stops_late_side_effect_and_later_stages(tmp_path, monkeypatch):
    marker=tmp_path/'late'
    source='''import time
from pathlib import Path
def detect(context):
    time.sleep(.9)
    Path(context['payload']['marker']).write_text('late')
    return {'status':'matched'}
def diagnose(context):
    raise AssertionError('must not run')
'''
    executor=make_executor(tmp_path,monkeypatch,source)
    start=time.monotonic()
    run=executor.run('test.deadline', {'marker':str(marker)})
    assert time.monotonic()-start < .8
    assert run.status=='failed'
    assert run.stages['detect'].error=='skill_stage_timeout'
    assert 'diagnose' not in run.stages
    time.sleep(1)
    assert not marker.exists()


def test_result_and_context_survive_process_boundary(tmp_path,monkeypatch):
    executor=make_executor(tmp_path,monkeypatch,'''
def detect(context):
    context['cached_value']=41
    print('ordinary skill output must not corrupt IPC')
    return {'status':'matched', 'confidence':.9}
def diagnose(context):
    return {'status':'ok','answer':context['cached_value']+1}
''',timeout='2')
    result=executor.run('test.deadline',{},stages=['detect','diagnose'])
    assert result.status=='completed'
    assert result.stages['diagnose'].output['answer']==42


def test_tool_timeout_records_unknown_and_never_replays(tmp_path,monkeypatch):
    executor=make_executor(tmp_path,monkeypatch,'''
def heal(context):
    context['tools'].execute('test.external_write', value=1)
    return {'status':'ok', 'executed':True}
''')
    calls=[]
    def runner(tool,args,payload):
        calls.append(tool)
        time.sleep(.8)
        return {'success':True,'data':'late'}
    run=executor.run('test.deadline',{},stages=['heal'],dry_run=False,tool_runner=runner)
    snapshot=run.to_dict()
    assert run.status=='failed'
    assert run.stages['heal'].output['outcome_unknown'] is True
    time.sleep(1)
    assert run.to_dict()==snapshot
    assert calls==['test.external_write']


def test_successful_tool_runs_in_host_and_trace_is_preserved(tmp_path,monkeypatch):
    executor=make_executor(tmp_path,monkeypatch,'''
def detect(context):
    result=context['tools'].execute('kubectl', command='get pods')
    return {'status':'matched' if result['success'] else 'error'}
''',timeout='2')
    calls=[]
    def runner(tool,args,payload):
        calls.append((os.getpid(),tool))
        return {'success':True, 'data':'pods'}
    run=executor.run('test.deadline',{},stages=['detect'],tool_runner=runner)
    assert run.status=='completed'
    assert calls==[(os.getpid(),'kubectl')]
    assert run.trace[0]['tool_calls'][0]['success'] is True


def test_worker_crash_is_failure(tmp_path,monkeypatch):
    executor=make_executor(tmp_path,monkeypatch,"import os\ndef detect(context):\n    os._exit(7)\n",timeout='2')
    result=executor.run('test.deadline',{},stages=['detect'])
    assert result.status=='failed'
    assert result.stages['detect'].error=='skill_worker_crashed'


def test_matching_preserves_failed_probe(tmp_path,monkeypatch):
    executor=make_executor(tmp_path,monkeypatch,"import time\ndef detect(context):\n    time.sleep(.9)\n    return {'status':'matched'}\n")
    result=executor.run_matching({},domain='test',stages=['detect'])
    assert result['matched_count']==0
    assert result['failed_runs'][0]['stages']['detect']['error']=='skill_stage_timeout'


def test_timeout_kills_skill_subprocesses(tmp_path,monkeypatch):
    marker=tmp_path/'descendant'
    executor=make_executor(tmp_path,monkeypatch,'''
import subprocess, sys, time
def detect(context):
    subprocess.Popen([sys.executable, '-c', "import time; from pathlib import Path; time.sleep(.9); Path("+repr(context['payload']['marker'])+").write_text('late')"])
    time.sleep(10)
''')
    result=executor.run('test.deadline',{'marker':str(marker)},stages=['detect'])
    assert result.stages['detect'].error=='skill_stage_timeout'
    time.sleep(1)
    assert not marker.exists()


def test_failed_workflow_is_not_silently_replaced_by_legacy(tmp_path,monkeypatch):
    from skill_runtime.workflow import SkillWorkflow
    executor=make_executor(tmp_path,monkeypatch,"import time\ndef detect(context):\n    time.sleep(.9)\n    return {'status':'matched'}\n")
    workflow=SkillWorkflow(executor)
    calls=[]
    monkeypatch.setattr(workflow,'_legacy_plan',lambda payload: calls.append(payload) or {'status':'ok'})
    result=workflow.run({},domain='test',stages=['detect'],dry_run=False)
    assert result['status']=='failed'
    assert result['requires_manual_review'] is True
    assert result['capability']['auto_execution_eligible'] is False
    assert result['skill_runtime']['failed_runs']
    assert calls==[]


def test_matching_reuses_one_worker_without_repeated_startup(tmp_path,monkeypatch):
    executor=make_executor(tmp_path,monkeypatch,"import os\ndef detect(context):\n    return {'status':'matched', 'pid':os.getpid()}\n",timeout='2')
    second=tmp_path/'second'
    second.mkdir()
    (second/'skill.yaml').write_text('id: test.second\nversion: 1.0\nname: second\ndomain: test\nstages: [detect]\n')
    (second/'skill.py').write_text((tmp_path/'sample'/'skill.py').read_text())
    executor.registry.load()
    result=executor.run_matching({},domain='test',stages=['detect'])
    pids=[run['stages']['detect']['output']['pid'] for run in result['runs']]
    assert len(pids)==2 and len(set(pids))==1
    assert pids[0] != os.getpid()


def test_matching_restarts_worker_after_timeout_for_next_skill(tmp_path,monkeypatch):
    executor=make_executor(tmp_path,monkeypatch,"import time\ndef detect(context):\n    time.sleep(5)\n")
    second=tmp_path/'second'
    second.mkdir()
    (second/'skill.yaml').write_text('id: test.second\nversion: 1.0\nname: second\ndomain: test\nstages: [detect]\n')
    (second/'skill.py').write_text("def detect(context):\n    return {'status':'matched'}\n")
    executor.registry.load()
    result=executor.run_matching({},domain='test',stages=['detect'])
    assert result['matched_count']==1 and len(result['failed_runs'])==1
    assert result['runs'][0]['skill']['id']=='test.second'


def test_verify_timeout_closes_direct_execution_policy(tmp_path,monkeypatch):
    executor=make_executor(tmp_path,monkeypatch,'''
import time
def detect(context):
    return {'status':'matched'}
def diagnose(context):
    return {'status':'ok','confidence':.99,'evidence':['actual observation']}
def heal(context):
    return {'status':'ok'}
def verify(context):
    time.sleep(5)
''')
    result=executor.run('test.deadline',{})
    assert result.status=='failed'
    assert result.stages['verify'].error=='skill_stage_timeout'
    assert result.policy['heal_ready'] is False
    assert result.policy['auto_heal_ready'] is False
    assert 'skill_execution_failed' in result.policy['heal_blockers']
