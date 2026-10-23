"""Bounded read-only acquisition of source files declared by a target Pod.

ConfigMap contents describe declared code, not proof of loaded process state.
Secret volumes, environment values and unrelated ConfigMaps are never queried.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from agents.evidence import make_evidence_item
from tools.llm_client import run_sync_in_daemon_thread

_NAME = re.compile(r'[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?')
_SOURCE = re.compile(r'^[A-Za-z0-9_.-]+\.(?:py|js|ts|go|java)$')


async def collect_workload_artifacts(registry, namespace, pod_name):
    return await run_sync_in_daemon_thread(_collect, registry, namespace, pod_name)


def _collect(registry, namespace, pod_name):
    tool = registry.get('kubectl')
    if not tool or not _NAME.fullmatch(namespace) or not _NAME.fullmatch(pod_name):
        return dict(agent='configuration_agent', error=True, evidence_items=[], summary='目标配置范围无效。')

    def read(kind, name):
        response = tool.execute(command=f'get {kind} {name} -o json', namespace=namespace)
        if not response.success:
            return {}
        try:
            data = json.loads(response.data) if isinstance(response.data, str) else response.data
        except (ValueError, TypeError):
            return {}
        if (not isinstance(data, dict) or data.get('metadata', {}).get('namespace') != namespace
                or data.get('metadata', {}).get('name') != name):
            return {}
        return data

    pod = read('pod', pod_name)
    if not pod:
        return dict(agent='configuration_agent', error=True, evidence_items=[], summary='未取得目标 Pod 配置。')
    stamp = datetime.now(timezone.utc).isoformat()
    spec = pod.get('spec', {})
    containers = spec.get('containers', [])
    # Include field names only; arbitrary environment/command values may carry credentials.
    facts = [make_evidence_item('pod_declared_configuration', json.dumps({
        'containers': [{'name': c.get('name'), 'image': c.get('image'),
            'environment_names': [e.get('name') for e in c.get('env', [])],
            'env_from_present': bool(c.get('envFrom')),
            'mounted_volumes': [m.get('name') for m in c.get('volumeMounts', [])]} for c in containers],
        'interpretation': '声明配置，不等于进程实际加载状态；未列出的环境变量仍可能来自镜像或 envFrom。'
    }, ensure_ascii=False), service=pod_name, source='kubernetes_manifest', timestamp=stamp,
        raw_ref={'namespace': namespace, 'pod': pod_name, 'uid': pod['metadata'].get('uid'),
                 'resource_version': pod['metadata'].get('resourceVersion')})]
    mounted = {m.get('name') for c in containers for m in c.get('volumeMounts', [])}
    maps = []
    for volume in spec.get('volumes', []):
        name = (volume.get('configMap') or {}).get('name', '')
        if volume.get('name') in mounted and _NAME.fullmatch(name) and name not in maps:
            maps.append(name)
    for name in maps[:2]:
        config = read('configmap', name)
        for key, source in list(config.get('data', {}).items())[:20]:
            if not _SOURCE.fullmatch(key) or not isinstance(source, str) or len(source) > 32000:
                continue
            # Do not deliver obvious embedded credentials as reasoning context.
            if re.search(r'sk-[A-Za-z0-9]{20,}|(?i:password|api[_-]?key|secret|token)\s*=\s*[\'\"][^\'\"]+', source):
                continue
            lines = source.splitlines()
            anchors = [i for i, line in enumerate(lines) if re.search(r'\b5\d\d\b', line)]
            if not anchors:
                continue
            indexes = set()
            for i in anchors[:2]:
                indexes.update(range(max(0, i-6), min(len(lines), i+16)))
            # Assignments provide context for branch variables without executing code.
            indexes.update(i for i, line in enumerate(lines) if re.match(r'^\s*[A-Z_]+\s*=', line))
            selected = [f'{i+1}: {lines[i]}' for i in sorted(indexes)]
            chunks, chunk = [], ''
            for line in selected:
                if len(chunk) + len(line) > 950:
                    chunks.append(chunk); chunk = ''
                chunk += line[:500] + '\n'
            if chunk: chunks.append(chunk)
            digest = hashlib.sha256(source.encode()).hexdigest()
            for index, excerpt in enumerate(chunks[:3]):
                facts.append(make_evidence_item('declared_source_excerpt', excerpt,
                    service=pod_name, source='kubernetes_configmap', timestamp=stamp,
                    raw_ref={'namespace': namespace, 'configmap': name, 'key': key,
                        'resource_version': config['metadata'].get('resourceVersion'),
                        'sha256': digest, 'part': index+1,
                        'scope': 'declared source only; runtime state and change actor unverified'}))
            break
    return dict(agent='configuration_agent', evidence_items=facts, partial=True,
        summary=f'取得 {len(facts)} 条目标 Pod 声明配置/源代码观测；不证明加载版本、运行态变量或变更操作者。')
