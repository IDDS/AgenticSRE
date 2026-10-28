"""Bounded Service/Deployment selector comparison through observed UID owners."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
import json
import time

from operational_ontology import committed_tool_observations, observation_id
from skill_runtime.service_evidence import observed_entity_catalog, projection_command

MAX_PODS = 6
MAX_REPLICASETS = 3


def merge_cited_catalog(existing, fresh, hypotheses, items, limit=24):
    """Keep all asserted citations and selected owner-chain references, or abstain."""
    merged={row['id']:row for row in [*existing,*fresh]}
    prior={ref for h in hypotheses for ref in [*(h.get('supporting_evidence_ids') or []),
                                               *(h.get('contradicting_evidence_ids') or [])]}
    required=prior | {ref for item in items for ref in item.get('observation_ids') or []}
    complete=len(required)<=limit and required<=set(merged)
    if not complete: required=prior
    ordered=[row for row in merged.values() if row['id'] in required]
    ordered.extend(row for row in merged.values() if row['id'] not in required)
    return ordered[:limit],complete


def collect_selector_ownership(*, commands, hypotheses, root_entity, service_scope, service, cluster_id,
                               namespace, run_read, per_read_timeout_s=5, total_budget_s=25):
    result = {'schema':'selector-ownership-evidence/v1','attempted':False,'items':[],
        'stop_reason':'no_selector_question','reads':[], 'absence_is_refutation':False,
        'incident_window_bound':False,'causality_proven':False,'execution_authorized':False}
    questions = [h for h in hypotheses if h.get('status')=='active' and h.get('proposed_status')=='rejected'
        and 'unverified_rejection' in (h.get('validation_errors') or [])
        and ('selector' in str(h.get('description') or '').casefold() or '选择器' in str(h.get('description') or ''))
        and ('label' in str(h.get('description') or '').casefold() or '标签' in str(h.get('description') or ''))]
    if not questions:
        return result, []
    identities = observed_entity_catalog(commands, cluster_id, namespace)
    service_ids = {(r.get('uid'),r.get('resource_version')) for r in identities
        if r.get('kind')=='Service' and r.get('name')==service and r.get('uid') and r.get('resource_version')}
    if (not isinstance(root_entity,dict) or root_entity.get('kind')!='Deployment'
            or root_entity.get('cluster_id')!=cluster_id or root_entity.get('namespace')!=namespace
            or not root_entity.get('uid') or not root_entity.get('resource_version')
            or not isinstance(service_scope,dict) or service_scope.get('status')!='collected'
            or service_scope.get('candidate_workloads')!=[root_entity.get('name')] or len(service_ids)!=1):
        result['stop_reason']='selector_scope_unresolved'
        return result, []
    expected_service = next(iter(service_ids))
    try:
        service_command=projection_command('Service',namespace,service)
        deployment_command=projection_command('DeploymentSelector',namespace,root_entity['name'])
    except (ValueError,TypeError,KeyError):
        result['stop_reason']='selector_scope_unresolved'
        return result, []
    deadline=time.monotonic()+total_budget_s
    result['attempted']=True
    reads=[]
    def read(command):
        remaining=min(per_read_timeout_s,deadline-time.monotonic())
        if remaining<=0:
            raise RuntimeError('selector_budget_exhausted')
        pool=ThreadPoolExecutor(max_workers=1)
        future=pool.submit(run_read,command)
        try: row=future.result(timeout=remaining)
        except TimeoutError:
            future.cancel()
            raise RuntimeError('selector_read_timeout')
        except Exception:
            raise RuntimeError('selector_read_failed')
        finally: pool.shutdown(wait=False,cancel_futures=True)
        if not isinstance(row,dict): raise RuntimeError('selector_read_failed')
        row={**row,'timestamp':datetime.now(timezone.utc).isoformat(),'cluster_id':cluster_id,'namespace':namespace}
        reads.append(row)
        result['reads'].append({'command':command,'executed':row.get('executed') is True,'success':row.get('success') is True})
        if row.get('executed') is not True or row.get('success') is not True or row.get('outcome_unknown'):
            raise RuntimeError('selector_read_failed')
        try: data=json.loads(row.get('data') or '')
        except (ValueError,TypeError): raise RuntimeError('selector_projection_invalid')
        if not isinstance(data,dict) or data.get('projection')!='service-evidence/v1':
            raise RuntimeError('selector_projection_invalid')
        return data,row
    def identity(data,kind,name,uid,version):
        meta=data.get('metadata') or {}
        return (data.get('kind')==kind and meta.get('name')==name and meta.get('namespace')==namespace
                and meta.get('uid')==uid and meta.get('resourceVersion')==version)
    def obs_id(row,kind,name,uid):
        matches=[o for o in committed_tool_observations(row) if (o.get('raw_ref') or {}).get('kind')==kind
            and ((o.get('raw_ref') or {}).get('metadata') or {}).get('name')==name
            and ((o.get('raw_ref') or {}).get('metadata') or {}).get('uid')==uid]
        return observation_id(matches[0]) if len(matches)==1 else None
    try:
        service_data,service_row=read(service_command)
        if not identity(service_data,'Service',service,*expected_service): raise RuntimeError('selector_service_changed')
        service_selector=(service_data.get('spec') or {}).get('selector')
        if (not isinstance(service_selector,dict) or not service_selector
                or (service_data.get('spec') or {}).get('type')=='ExternalName'):
            raise RuntimeError('selector_service_unsupported')
        deployment_data,deployment_row=read(deployment_command)
        if not identity(deployment_data,'Deployment',root_entity['name'],root_entity['uid'],root_entity['resource_version']):
            raise RuntimeError('selector_deployment_changed')
        selector=(deployment_data.get('spec') or {}).get('selector') or {}
        labels=selector.get('matchLabels') if isinstance(selector,dict) else None
        expressions=selector.get('matchExpressions') if isinstance(selector,dict) else None
        if not isinstance(labels,dict) or not labels or expressions not in (None,[]):
            raise RuntimeError('selector_deployment_unsupported')
        pods,pod_row=read(projection_command('Pod',namespace,selector=labels))
        items=pods.get('items')
        if pods.get('kind')!='List' or pods.get('inventory_complete') is not True or not isinstance(items,list):
            raise RuntimeError('selector_pod_inventory_incomplete')
        if len(items)>MAX_PODS: raise RuntimeError('selector_pod_limit_exceeded')
        owners={}
        for pod in items:
            meta=pod.get('metadata') if isinstance(pod,dict) else None
            if (not isinstance(meta,dict) or meta.get('namespace')!=namespace
                    or not all(isinstance(meta.get(k),str) and meta[k] for k in ('name','uid','resourceVersion'))
                    or not isinstance(meta.get('labels'),dict)
                    or any(meta['labels'].get(k)!=v for k,v in labels.items())):
                raise RuntimeError('selector_pod_inventory_conflict')
            refs=meta.get('ownerReferences') if isinstance(meta,dict) else None
            if not isinstance(refs,list): continue
            controller=[r for r in refs if isinstance(r,dict) and r.get('controller') is True]
            if (len(controller)==1 and controller[0].get('kind')=='ReplicaSet'
                    and all(isinstance(controller[0].get(k),str) and controller[0][k] for k in ('name','uid'))):
                owners[(controller[0]['name'],controller[0]['uid'])]=None
        if len(owners)>MAX_REPLICASETS: raise RuntimeError('selector_owner_limit_exceeded')
        for name,uid in owners:
            replica,replica_row=read(projection_command('ReplicaSet',namespace,name))
            meta=replica.get('metadata') or {}
            if replica.get('kind')!='ReplicaSet' or meta.get('namespace')!=namespace or meta.get('name')!=name or meta.get('uid')!=uid:
                raise RuntimeError('selector_owner_changed')
            controllers=[r for r in meta.get('ownerReferences',[]) if isinstance(r,dict) and r.get('controller') is True]
            if (len(controllers)==1 and controllers[0].get('kind')=='Deployment'
                    and controllers[0].get('name')==root_entity['name'] and controllers[0].get('uid')==root_entity['uid']):
                owners[(name,uid)]=replica_row
        last_service,_=read(service_command)
        last_deployment,_=read(deployment_command)
        if not identity(last_service,'Service',service,*expected_service): raise RuntimeError('selector_service_changed')
        if not identity(last_deployment,'Deployment',root_entity['name'],root_entity['uid'],root_entity['resource_version']):
            raise RuntimeError('selector_deployment_changed')
        service_ref=obs_id(service_row,'Service',service,expected_service[0])
        deployment_ref=obs_id(deployment_row,'Deployment',root_entity['name'],root_entity['uid'])
        for pod in items:
            meta=pod.get('metadata') if isinstance(pod,dict) else None
            if not isinstance(meta,dict) or not isinstance(meta.get('labels'),dict): continue
            refs=meta.get('ownerReferences') or []
            controller=[r for r in refs if isinstance(r,dict) and r.get('controller') is True]
            if len(controller)!=1 or controller[0].get('kind')!='ReplicaSet': continue
            owner=controller[0]; replica_row=owners.get((owner.get('name'),owner.get('uid')))
            if not replica_row: continue
            pod_ref=obs_id(pod_row,'Pod',meta.get('name'),meta.get('uid'))
            replica_ref=obs_id(replica_row,'ReplicaSet',owner.get('name'),owner.get('uid'))
            if not all((service_ref,deployment_ref,pod_ref,replica_ref)): continue
            mismatched=sorted(k for k,v in service_selector.items() if meta['labels'].get(k)!=v)
            if mismatched:
                result['items'].append({'pod':meta['name'],'pod_uid':meta['uid'],
                    'replicaset':owner['name'],'replicaset_uid':owner['uid'],'mismatched_keys':mismatched,
                    'observation_ids':[service_ref,deployment_ref,replica_ref,pod_ref],
                    'snapshot_consistency':'not_atomic','causality_proven':False})
        result['stop_reason']='selector_observations_collected'
    except (RuntimeError,ValueError,TypeError,KeyError) as exc:
        result['items']=[]
        result['stop_reason']=str(exc) if str(exc).startswith('selector_') else 'selector_projection_invalid'
    return result,reads
