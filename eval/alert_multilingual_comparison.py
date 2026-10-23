"""Remote comparison of frozen retrieval probes and independent controls."""
import argparse
import copy
import json
from pathlib import Path
import time

from agents.alert_compression import compress_alerts
from agents.alert_semantics import recall_candidates, local_embeddings, build_scag
import alert_compression_exploration as exploration
import alert_evidence_comparison as evidence


def controls(encoder):
    # Written before observing either model's results; each pair has an isolated
    # namespace and different entities. These are retrieval labels, not causes.
    positives = [
        ('dns','DNS lookup failed for the upstream hostname','上游主机名称解析失败'),
        ('permission','Access denied when opening the configuration file','打开配置文件时被拒绝访问'),
        ('quota','The namespace has exhausted its resource quota','命名空间的资源配额已耗尽'),
        ('mount','Unable to mount the persistent volume','持久化卷挂载失败'),
        ('leader','Leader election failed because the lease expired','租约过期导致领导者选举失败'),
        ('queue','The message queue backlog is growing','消息队列中积压的消息持续增加'),
        ('deadlock','Database transaction aborted due to a deadlock','数据库事务因死锁而中止'),
        ('ratelimit','Requests were rejected because the rate limit was exceeded','请求频率超过限制而被拒绝'),
        ('cpu','The container is being throttled by its CPU limit','容器受到 CPU 配额限制而被节流'),
        ('port','The process cannot bind to the port because it is already in use','端口已被占用，进程无法绑定'),
        ('auth','Authentication failed because the credentials are invalid','凭据无效导致身份认证失败'),
        ('eviction','The pod was evicted because the node had insufficient disk space','节点磁盘空间不足导致容器组被驱逐'),
    ]
    unrelated = [
        ('storage-auth','The filesystem has no space left','用户成功完成身份认证'),
        ('certificate-backup','The TLS certificate is expired','每日数据库备份已成功完成'),
        ('dns-memory','Hostname resolution failed','容器内存使用率正常'),
        ('image-queue','The image tag does not exist','消息队列已清空'),
        ('cpu-volume','The container reached its CPU limit','持久化卷挂载成功'),
        ('port-leader','The port is already occupied','集群领导者选举成功'),
        ('en-unrelated','A scheduled backup completed successfully','The TLS certificate has expired'),
        ('zh-unrelated','用户成功修改了密码','磁盘容量耗尽，无法写入'),
        ('en-different-failure','The filesystem is full','Authentication credentials are invalid'),
        ('zh-different-failure','进程因内存耗尽被终止','服务端证书已过期'),
        ('mixed-different-failure','The container image tag is missing','数据库事务发生死锁'),
        ('mixed-different-failure-2','The TLS certificate is invalid','资源配额已经耗尽'),
    ]
    cases = []
    for kind, entries in [('positive', positives), ('unrelated', unrelated)]:
        for name, left, right in entries:
            records = [exploration.record(i, pod=f'object-{i}', message=t) for i,t in enumerate([left,right])]
            original = compress_alerts(records)
            result = recall_candidates(copy.deepcopy(original), encoder=encoder)
            build_scag(result)
            assert result['groups'] == original['groups'] and result['raw_alerts'] == original['raw_alerts']
            assert not any(e['causal_confirmed'] for e in result['scag']['edges'])
            cases.append(dict(name=name, kind=kind, left=left, right=right,
                              retrieved=bool(result['candidate_pairs'])))
    return {'cases':cases, 'positive_hits':sum(c['retrieved'] for c in cases if c['kind']=='positive'),
            'positive_total':len(positives),
            'unrelated_candidates':sum(c['retrieved'] for c in cases if c['kind']=='unrelated'),
            'unrelated_total':len(unrelated)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--profile', choices=['legacy','multilingual','configured'], required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    if args.profile == 'multilingual':
        from agents.alert_embeddings import MultilingualEncoder
        encoder = MultilingualEncoder('/model')
    else:
        encoder = local_embeddings
    encoder(['model initialization probe'])
    report = {'profile':args.profile, 'initialization_seconds':round(time.monotonic()-start,3)}
    def recall(result, **kwargs):
        return recall_candidates(result, encoder=encoder, **kwargs)
    exploration.recall_candidates = evidence.recall_candidates = recall
    exploration.local_embeddings = encoder
    start = time.monotonic()
    report['frozen_joint_scope'] = exploration.retrieval_probe()
    if args.profile == 'configured':
        from agents.alert_embeddings import embedding_metadata
        channels = embedding_metadata()['embedding_channels']
        if len(channels) > 1:
            import numpy as np
            for case in report['frozen_joint_scope']['pairs']:
                vectors = np.asarray(encoder([case['left'],case['right']]))
                scores = []
                for v in np.split(vectors,len(channels),axis=1):
                    v /= np.linalg.norm(v,axis=1)[:,None]
                    scores.append(float(v[0] @ v[1]))
                case['channel_cosines'] = dict(zip(channels,scores))
                case['cosine'] = round(max(scores),6)
    report['frozen_joint_scope_seconds'] = round(time.monotonic()-start,3)
    evidence.retrieval_challenge(args.output)
    report['frozen_isolated'] = json.loads((args.output/'retrieval-challenge.json').read_text())
    report['independent_controls'] = controls(encoder)
    report['interpretation'] = 'Hand-authored semantic retrieval cases; no production accuracy or causal accuracy claim.'
    (args.output/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ['frozen_isolated','frozen_joint_scope']},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
