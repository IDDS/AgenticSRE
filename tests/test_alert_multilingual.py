"""Offline model selection, vector-space isolation, and bounded hybrid recall."""
import copy
from concurrent.futures import ThreadPoolExecutor
import pytest

from agents.alert_compression import compress_alerts
import agents.alert_semantics as semantics


def scan(count=2):
    return compress_alerts([dict(name='ApplicationLog', source='logs', message=text,
        cluster_id='qa', namespace='shop', pod=f'pod-{i}', timestamp=1000)
        for i,text in enumerate(['alpha', 'bravo'] if count == 2 else [f'word{i}' for i in range(count)])])


@pytest.mark.parametrize('profile', ['paraphrase-multilingual-MiniLM-L12-v2', 'unsupported-model'])
def test_missing_selected_model_never_uses_old_space(monkeypatch, tmp_path, profile):
    monkeypatch.setenv('AGENTICSRE_ALERT_EMBEDDING_MODEL', profile)
    monkeypatch.setenv('AGENTICSRE_ALERT_MULTILINGUAL_PATH', str(tmp_path/'missing'))
    result = semantics.recall_candidates(scan())
    assert result['retrieval']['status'] == 'lexical_fallback'
    assert result['retrieval']['encoder'] == profile
    assert not result['candidate_pairs']


def test_injected_encoder_provenance_is_not_mislabeled():
    result = semantics.recall_candidates(scan(), encoder=lambda texts:[[1,0] for _ in texts])
    assert result['retrieval']['encoder'] == 'injected'


def hybrid(monkeypatch, vectors):
    import agents.alert_embeddings as embeddings
    monkeypatch.setenv('AGENTICSRE_ALERT_EMBEDDING_MODEL', embeddings.HYBRID)
    roots = ((embeddings.LEGACY, '/app/models/'+embeddings.LEGACY),
             (embeddings.MULTILINGUAL, '/app/models/'+embeddings.MULTILINGUAL))
    monkeypatch.delenv('AGENTICSRE_ALERT_EMBEDDING_PATH', raising=False)
    monkeypatch.delenv('AGENTICSRE_ALERT_MULTILINGUAL_PATH', raising=False)
    monkeypatch.setattr(embeddings, '_identity', roots)
    monkeypatch.setattr(embeddings, '_cache', embeddings.OrderedDict())
    # Deliberately different magnitudes demonstrate separate normalization.
    monkeypatch.setattr(embeddings, '_encoders', [
        lambda texts:[vectors[t][:2] for t in texts],
        lambda texts:[vectors[t][2:] for t in texts]])


@pytest.mark.parametrize('right,expected', [([1,0,0,50],True), ([0,1,50,0],True), ([0,1,0,50],False)])
def test_hybrid_preserves_each_channel_without_averaging(monkeypatch, right, expected):
    hybrid(monkeypatch, {'alpha':[1,0,50,0], 'bravo':right})
    original = scan()
    result = semantics.recall_candidates(copy.deepcopy(original))
    assert bool(result['candidate_pairs']) is expected
    assert result['retrieval']['cosine_aggregation'] == 'maximum_per_channel'
    assert result['raw_alerts'] == original['raw_alerts'] and result['groups'] == original['groups']
    semantics.build_scag(result)
    assert not any(edge['causal_confirmed'] for edge in result['scag']['edges'])


@pytest.mark.parametrize('field,value', [('cluster_id','other'),('namespace','other'),('first_seen',2000)])
def test_hybrid_cannot_cross_scope_or_time(monkeypatch, field, value):
    hybrid(monkeypatch, {'alpha':[1,0,50,0], 'bravo':[1,0,50,0]})
    result = scan()
    result['groups'][1][field] = value
    if field == 'first_seen':
        result['groups'][1]['last_seen'] = value
    assert not semantics.recall_candidates(result)['candidate_pairs']


def test_large_hybrid_queries_each_space_and_keeps_caps(monkeypatch):
    import agents.alert_retrieval as retrieval
    import numpy as np
    hybrid(monkeypatch, {f'word{i}':[1,0,0,10] for i in range(257)})
    queries = []
    def pools(groups,tokens,tags,vectors,top_n):
        queries.append(vectors.copy())
        offset = len(queries)
        return [{(i+offset)%len(groups)} for i in range(len(groups))], {
            'search_mode':'test', 'neighbor_pool_limit':1, 'queried_groups':len(groups),
            'approximate':False, 'ann_query_vectors':0, 'exact_query_vectors':len(groups)}
    monkeypatch.setattr(retrieval, 'neighbor_pools', pools)
    result = semantics.recall_candidates(scan(257),top_n=1,max_pairs=7)
    assert len(queries) == 2 and queries[0].shape == queries[1].shape == (257,2)
    assert np.allclose(queries[0][0],[1,0]) and np.allclose(queries[1][0],[0,1])
    assert len(result['candidate_pairs']) == 7
    assert result['retrieval']['candidate_pairs_truncated']
    assert result['retrieval']['neighbor_pool_limit'] == 2


def test_switching_paths_cannot_reuse_cached_vectors(monkeypatch, tmp_path):
    import agents.alert_embeddings as embeddings
    monkeypatch.setenv('AGENTICSRE_ALERT_EMBEDDING_MODEL', embeddings.MULTILINGUAL)
    monkeypatch.setattr(embeddings, '_identity', None)
    monkeypatch.setattr(embeddings, '_cache', embeddings.OrderedDict())
    def load(root):
        if root.endswith('missing'):
            raise FileNotFoundError('test model absent')
        return lambda texts:[[1,0] if root.endswith('first') else [0,1] for _ in texts]
    monkeypatch.setattr(embeddings, 'MultilingualEncoder', load)
    for suffix, expected in [('first',(1.,0.)),('second',(0.,1.))]:
        monkeypatch.setenv('AGENTICSRE_ALERT_MULTILINGUAL_PATH',str(tmp_path/suffix))
        assert embeddings.local_embeddings(['same text']) == [expected]
    monkeypatch.setenv('AGENTICSRE_ALERT_MULTILINGUAL_PATH',str(tmp_path/'missing'))
    with pytest.raises(FileNotFoundError):
        embeddings.local_embeddings(['same text'])
    assert not embeddings._cache


def test_concurrent_duplicates_only_encode_once(monkeypatch, tmp_path):
    import agents.alert_embeddings as embeddings
    monkeypatch.setenv('AGENTICSRE_ALERT_EMBEDDING_MODEL', embeddings.MULTILINGUAL)
    monkeypatch.setenv('AGENTICSRE_ALERT_MULTILINGUAL_PATH',str(tmp_path))
    monkeypatch.setattr(embeddings, '_identity', None)
    monkeypatch.setattr(embeddings, '_cache', embeddings.OrderedDict())
    calls = []
    def load(root):
        def encode(texts):
            calls.extend(texts)
            return [[1,0] for _ in texts]
        return encode
    monkeypatch.setattr(embeddings, 'MultilingualEncoder', load)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _:embeddings.local_embeddings(['same','same']),range(32)))
    assert all(r == [(1.,0.),(1.,0.)] for r in results)
    assert calls == ['same']


def test_tampered_offline_model_is_rejected(tmp_path):
    from agents.alert_embeddings import MultilingualEncoder
    (tmp_path/'onnx').mkdir()
    (tmp_path/'onnx/model.onnx').write_bytes(b'corrupted model')
    with pytest.raises(ValueError,match='checksum mismatch'):
        MultilingualEncoder(tmp_path)
