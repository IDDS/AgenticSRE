"""Project observed container failures without turning previous state into current state."""
from datetime import datetime, timezone
import re


MESSAGE_LIMIT = 2000


def pod_spec_checks(pod):
    """Project a full Pod API response into two narrow, read-only tests.

    No volume contents, env, commands, Secret references or arbitrary metadata
    are retained. Unknown, malformed or oversized inputs cannot prove absence.
    Zero is checked across every request source, not via a naive resource sum.
    """
    if not isinstance(pod,dict):
        return None
    meta,spec,status=(pod.get(k) for k in ('metadata','spec','status'))
    if not all(isinstance(v,dict) for v in (meta,spec,status)):
        return None
    if not all(isinstance(meta.get(k),str) and meta[k] for k in ('name','namespace','uid','resourceVersion')):
        return None
    if pod.get('kind','Pod')!='Pod' or meta.get('deletionTimestamp'):
        return None
    def zero(value):
        if isinstance(value,bool) or not isinstance(value,(str,int,float)):
            return None
        text=str(value)
        if len(text)>80 or not re.fullmatch(r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+|[numkKMGTPE]i?)?',text):
            return None
        return bool(re.fullmatch(r'(?:0+(?:\.0*)?|\.0+)(?:[eE][+-]?\d+|[numkKMGTPE]i?)?',text))
    request_rows=[]
    def requests(resources,label):
        if not isinstance(resources,dict) or not isinstance(resources.get('requests',{}),dict):
            return None
        req=resources.get('requests',{})
        values={k:req.get(k,'0') for k in ('cpu','memory')}
        states=[zero(v) for v in values.values()]
        limits=resources.get('limits',{})
        if not isinstance(limits,dict):
            states.append(None)
        elif any(k not in req and k in limits and zero(limits[k]) is not True for k in ('cpu','memory')):
            states.append(None)
        request_rows.append({'scope':label,**{k:str(v)[:80] for k,v in values.items()}})
        return None if None in states else all(states)
    states=[]
    for field in ('containers','initContainers','ephemeralContainers'):
        rows=spec.get(field,[])
        if not isinstance(rows,list) or len(rows)>64 or (field=='containers' and not rows):
            states.append(None);continue
        for c in rows:
            if not isinstance(c,dict) or not isinstance(c.get('name'),str) or not c['name']:
                states.append(None);continue
            states.append(requests(c.get('resources',{}),field+'/'+c['name']))
    states.append(requests(spec.get('resources',{}),'pod'))
    overhead=spec.get('overhead',{})
    states.append(requests({'requests':overhead},'overhead'))
    # In-place resize/allocated requests may differ from desired spec values.
    if status.get('resize') or any(status.get(k) for k in ('containerStatuses','initContainerStatuses','ephemeralContainerStatuses')):
        states.append(None)
    all_zero=None if None in states else all(states)
    if status.get('phase')!='Pending' or spec.get('nodeName'):
        all_zero=None
    volumes=spec.get('volumes',[])
    volume_rows=[]
    no_binding=None
    if isinstance(volumes,list) and len(volumes)<=64:
        safe=True
        binding=False
        for volume in volumes:
            if not isinstance(volume,dict) or not isinstance(volume.get('name'),str):
                safe=False;continue
            kinds=[k for k in volume if k!='name']
            volume_rows.append({'name':volume['name'],'source_types':kinds})
            if len(kinds)!=1 or not isinstance(volume.get(kinds[0]),dict):
                safe=False;continue
            binding=binding or kinds[0] in ('persistentVolumeClaim','ephemeral')
            if kinds[0] not in ('persistentVolumeClaim','ephemeral','projected','secret','configMap','downwardAPI','emptyDir'):
                safe=False
        if safe:
            no_binding=not binding
    return {'schema':'pod-spec-checks/v1','phase':status.get('phase',''),
        'volumes':volume_rows,'cpu_memory_requests':request_rows,
        'checks':{'zero_cpu_memory_requests':all_zero,'no_pvc_or_ephemeral_volume':no_binding},
        'scope':'declared CPU/memory requests and PVC/ephemeral binding references only; not all resource/storage failures'}


def problem_containers(pods, namespace=""):
    observed_at = datetime.now(timezone.utc).isoformat()
    problems = []
    for pod in pods:
        if not isinstance(pod, dict):
            continue
        meta, status = pod.get('metadata') or {}, pod.get('status') or {}
        name, ns = meta.get('name'), meta.get('namespace') or namespace
        if not name or (namespace and ns != namespace):
            continue
        owner = next((o for o in meta.get('ownerReferences', [])
            if o.get('kind') == 'Job' and o.get('controller') is True), {})
        common = dict(name=name, namespace=ns, pod_uid=meta.get('uid', ''),
            resource_version=meta.get('resourceVersion', ''), observed_at=observed_at,
            node_name=(pod.get('spec') or {}).get('nodeName', ''),
            job_name=owner.get('name', ''), job_uid=owner.get('uid', ''))
        current = []
        history = []
        for field, kind in (('initContainerStatuses', 'init'), ('containerStatuses', 'application')):
            for container in status.get(field, []) or []:
                if not isinstance(container, dict):
                    continue
                ref = dict(common, container=container.get('name', ''), container_kind=kind,
                    restart_count=container.get('restartCount', 0))
                state = container.get('state') or {}
                for key, previous in (('terminated', False), ('waiting', False), ('terminated', True)):
                    value = (container.get('lastState') or {}).get(key) if previous else state.get(key)
                    if not isinstance(value, dict) or not value:
                        continue
                    if key == 'terminated' and value.get('exitCode') in (None, 0):
                        continue
                    message = value.get('message', '')
                    message = message if isinstance(message, str) else ''
                    item = dict(ref, state=key, previous=previous,
                        reason=value.get('reason') or ('Terminated' if key == 'terminated' else 'Waiting'),
                        message=message, message_truncated=len(message) > MESSAGE_LIMIT)
                    if key == 'terminated':
                        item.update(exit_code=value['exitCode'], signal=value.get('signal', 0),
                            started_at=value.get('startedAt', ''), finished_at=value.get('finishedAt', ''))
                    (history if previous else current).append(item)
        problems.extend(current)
        phase = status.get('phase', '')
        if phase not in ('Running', 'Succeeded') and not current:
            problems.append(dict(common, phase=phase, previous=False))
        # Historical failures remain audit context, including after recovery.
        problems.extend(history)
    return problems
