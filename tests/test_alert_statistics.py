import copy
import pytest
from agents.alert_compression import compress_alerts
from agents.alert_semantics import statistical_check


def fixture(offset=.025, right_values=None):
    values=[1,2,8,3,7,4,9,5,2,6]
    rows=[dict(cluster_id='c',namespace='ns',pod=pod,name='Metric',source='metrics',
               metric_name='response_error',metric_value=v,unit='ratio',timestamp=1000+i+(offset if pod=='b' else 0))
          for pod, vv in [('a',values),('b',right_values or values)] for i,v in enumerate(vv)]
    result=compress_alerts(rows)
    return result,*result['groups']


def test_realistic_timestamp_skew_aligns_and_retains_each_source_reference():
    result,a,b=fixture()
    before=copy.deepcopy(result)
    check=statistical_check(result,a,b)
    assert check['status']=='pass' and check['pearson']==1
    assert check['aligned_samples']==10 and check['coverage']==1
    assert check['max_offset_s']==pytest.approx(.025)
    assert len(check['matched_evidence'])==10
    assert result==before
    reverse=statistical_check(result,b,a)
    assert reverse['pearson']==check['pearson'] and reverse['aligned_samples']==10


@pytest.mark.parametrize('offset',[.11,.5,100])
def test_alignment_does_not_search_arbitrary_time_lags(offset):
    result,a,b=fixture(offset)
    assert statistical_check(result,a,b)['status']=='unknown'


def test_low_overlap_cannot_cherry_pick_a_correlated_subset():
    result,a,b=fixture()
    b['alert_indices']=b['alert_indices'][:5]
    check=statistical_check(result,a,b)
    assert check['aligned_samples']==5 and check['coverage']==.5
    assert check['status']=='unknown'


@pytest.mark.parametrize('mode',['constant','invalid','conflicting_duplicate'])
def test_bad_measurements_remain_unknown(mode):
    result,a,b=fixture()
    if mode=='constant':
        for index in b['alert_indices']:result['raw_alerts'][index]['raw']['metric_value']=1
    if mode=='invalid': result['raw_alerts'][b['alert_indices'][0]]['raw']['metric_value']='NaN'
    if mode=='conflicting_duplicate':
        row=copy.deepcopy(result['raw_alerts'][b['alert_indices'][0]])
        row['raw']['metric_value']=999
        b['alert_indices'].append(len(result['raw_alerts']));result['raw_alerts'].append(row)
    assert statistical_check(result,a,b)['status']=='unknown'


def test_alignment_never_optimizes_by_measurement_values():
    result,a,b=fixture(right_values=[9,8,2,7,3,6,1,5,8,4])
    check=statistical_check(result,a,b)
    assert check['aligned_samples']==10
    assert check['status']=='fail' and check['pearson']==-1


def test_duplicate_samples_are_not_counted_as_independent_observations():
    result,a,b=fixture()
    a['alert_indices'] *= 3
    check=statistical_check(result,a,b)
    assert check['aligned_samples']==10
    assert check['series_samples']==[10,10]
