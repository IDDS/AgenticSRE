"""Empty vector memory must not embed queries or download a model; run on snail."""
from types import SimpleNamespace

import pytest

from memory.fault_context_store import FaultContextStore


class Collection:
    def __init__(self, count=0):
        self.size = count
        self.calls = []

    def count(self):
        return self.size

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return {'documents': [['observed memory']] if self.size else [[]],
                'metadatas': [[{'status': 'active'}]] if self.size else [[]],
                'distances': [[.2]] if self.size else [[]]}


def store(rules=0, faults=0):
    value = object.__new__(FaultContextStore)
    value._backend = 'chromadb'
    value.mem_cfg = SimpleNamespace(max_similar_results=5)
    value._rules_col = Collection(rules)
    value._faults_col = Collection(faults)
    return value


@pytest.mark.parametrize('method,field', [('query_similar_rules', '_rules_col'),
                                         ('query_similar_faults', '_faults_col')])
def test_empty_collection_never_submits_embedding_query(method, field):
    memory = store()
    assert getattr(memory, method)('Pod Pending') == []
    assert getattr(memory, field).calls == []


@pytest.mark.parametrize('method,field', [('query_similar_rules', '_rules_col'),
                                         ('query_similar_faults', '_faults_col')])
def test_nonempty_collection_keeps_actual_vector_retrieval(method, field):
    memory = store(rules=2, faults=2)
    rows = getattr(memory, method)('Pod Pending', n=3)
    assert rows and rows[0]['text'] == 'observed memory'
    assert getattr(memory, field).calls == [{'query_texts': ['Pod Pending'], 'n_results': 3}]


def test_empty_combined_context_returns_explicit_zero_counts():
    memory = store()
    result = memory.get_historical_context('Pod Pending')
    assert {key: result[key] for key in ('rules', 'faults', 'rules_count', 'faults_count')} == {
        'rules': [], 'faults': [], 'rules_count': 0, 'faults_count': 0}
    assert result['retrieval']['status'] == 'complete'
    assert memory._rules_col.calls == memory._faults_col.calls == []


@pytest.mark.parametrize('method,field', [('query_similar_rules', '_rules_col'),
                                         ('query_similar_faults', '_faults_col')])
def test_count_failure_preserves_abstention_without_starting_embedding(method, field):
    memory = store()
    def fail():
        raise RuntimeError('controlled count failure')
    getattr(memory, field).count = fail
    assert getattr(memory, method)('Pod Pending') == []
    assert getattr(memory, field).calls == []


def test_actual_chroma_empty_collections_never_call_the_embedding_function(tmp_path):
    import chromadb
    calls = []
    class Embedding:
        def __call__(self, input):
            calls.append(list(input))
            return [[0.0, 0.0, 0.0] for value in input]
        @staticmethod
        def name():
            return 'controlled-empty-store-embedding'
    client = chromadb.PersistentClient(path=str(tmp_path / 'empty-chroma'))
    memory = store()
    memory._rules_col = client.get_or_create_collection('empty-rules', embedding_function=Embedding())
    memory._faults_col = client.get_or_create_collection('empty-faults', embedding_function=Embedding())
    assert memory.get_historical_context('Pod Pending')['rules_count'] == 0
    assert calls == []
