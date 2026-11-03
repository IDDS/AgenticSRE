"""Check signed changes, missing data and sparse windows."""
from .evidence import summarize_changes


def main():
    def item(name, cpu1, cpu2, lat1, lat2):
        return {'pod': name, 'metrics': {'before': {'CpuUsageRate(%)': {'mean': cpu1, 'n': 1}},
                                         'after': {'CpuUsageRate(%)': {'mean': cpu2, 'n': 2}}},
                'trace': {'before': {'p95_ms': lat1, 'spans': 50}, 'after': {'p95_ms': lat2, 'spans': 70}}}
    out = summarize_changes({'pods': [item('improved', 8, 6, 300, 100), item('worsened', 20, 80, 100, 1000), item('missing', None, 4, None, 10)]})
    assert out['trace_p95_change_ms'][0]['pod'] == 'worsened'
    assert out['trace_p95_change_ms'][1]['change']['delta'] == -200
    assert out['trace_p95_change_ms'][2]['change']['delta'] is None
    assert out['cpu_usage_rate_change_percentage_points'][0]['sparse'] is True
    try:
        summarize_changes({'pods': [item('x', 1, 2, 1, 2), item('x', 1, 2, 1, 2)]})
    except ValueError:
        pass
    else:
        raise AssertionError('duplicate pod accepted')
    print('incident replay evidence check: PASS')


if __name__ == '__main__':
    main()
