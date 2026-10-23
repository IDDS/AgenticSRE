"""Remote real-model load probe; no model stubs and no causal accuracy claim."""
import argparse
import copy
import json
from pathlib import Path
import resource
import time

from agents.alert_compression import compress_alerts
from agents.alert_semantics import recall_candidates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'provenance':'synthetic load only; unique text suffixes are not incident labels', 'cases':[]}
    for count, unique in [(10000,False),(1024,True)]:
        rows = [dict(name='ApplicationLog', source='logs', cluster_id='load', namespace='shop',
            pod=f'worker-{i}', timestamp=1700000000, severity='warning',
            message=('Database connection reset by peer' if i%2 else '数据库连接被对端重置')
                    + (f' shard={i}' if unique else '')) for i in range(count)]
        original = compress_alerts(rows)
        rounds = []
        for _ in range(2):
            started = time.monotonic()
            result = recall_candidates(copy.deepcopy(original))
            seconds = time.monotonic()-started
            assert result['groups'] == original['groups'] and result['raw_alerts'] == original['raw_alerts']
            assert result['retrieval']['status'] == 'ok'
            assert result['retrieval']['embedded_groups'] == count
            assert not result['retrieval']['truncated']
            assert len(result['candidate_pairs']) <= count*5
            rounds.append({'seconds':round(seconds,3), 'retrieval':result['retrieval']})
        report['cases'].append({'groups':count, 'unique_texts':count if unique else 2, 'rounds':rounds})
        report['peak_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report['cases'][-1]),flush=True)


if __name__ == '__main__':
    main()
