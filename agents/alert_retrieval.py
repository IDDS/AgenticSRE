"""Bounded neighbor pools for large alert scans; no changes to group membership."""
from bisect import bisect_left
from collections import defaultdict
import json
import uuid


def neighbor_pools(groups, tokens, tags, vectors, top_n):
    """Index every group, combine scoped ANN with bounded lexical neighbors.

    ANN is approximate. Neither a searched group nor a returned neighbor is a
    confirmed relation. Metadata filters enforce the same time/scope boundaries
    before ANN selection, so out-of-window hits cannot exhaust the neighbor pool.
    """
    postings = defaultdict(list)
    keys = []
    for i, group in enumerate(groups):
        scope = (group['cluster_id'], group['namespace'])
        terms = [('token', token) for token in tokens[i]] + [('ontology', tag) for tag in tags[i]]
        if group['entity']:
            terms.append(('entity', group['entity_kind'], group['entity']))
        row_keys = [scope + term for term in terms]
        keys.append(row_keys)
        for key in row_keys:
            postings[key].append(i)
    pools = [set() for _ in groups]
    for i, row_keys in enumerate(keys):
        # Rare terms first; no first-256 cutoff. Local windows also cover tails
        # of large duplicate-text buckets without an all-pairs Python loop.
        for key in sorted(row_keys, key=lambda key: (len(postings[key]), key)):
            values = postings[key]
            pos = bisect_left(values, i)
            for j in values[max(0, pos-16):pos+17]:
                if j != i:
                    pools[i].add(j)
                if len(pools[i]) >= 64:
                    break
            if len(pools[i]) >= 64:
                break
    info = {'search_mode': 'bounded_lexical', 'queried_groups': len(groups),
            'neighbor_pool_limit': 64, 'approximate': True}
    if vectors is None:
        return pools, info
    import chromadb
    from chromadb.config import Settings
    client = chromadb.EphemeralClient(Settings(anonymized_telemetry=False))
    name = 'alert-retrieval-' + uuid.uuid4().hex
    collection = client.create_collection(name, metadata={'hnsw:space': 'cosine'}, embedding_function=None)
    limit = max(32, min(128, top_n * 8))
    try:
        for start in range(0, len(groups), 256):
            batch = groups[start:start+256]
            collection.add(ids=[str(i) for i in range(start, start+len(batch))],
                           embeddings=vectors[start:start+len(batch)].tolist(),
                           metadatas=[{'scope': json.dumps([g['cluster_id'], g['namespace']]),
                                       'first': float(g['first_seen'] or 0), 'last': float(g['last_seen'] or 0)} for g in batch])
        batches = defaultdict(list)
        scopes = defaultdict(list)
        for i, g in enumerate(groups):
            if g['cluster_id'] and g['first_seen'] is not None and g['last_seen'] is not None:
                scopes[(g['cluster_id'], g['namespace'])].append(i)
                batches[(g['cluster_id'], g['namespace'], g['first_seen'], g['last_seen'])].append(i)
        queries = 0
        vector_queries = 0
        exact_queries = 0
        for (cluster, namespace, first, last), indices in batches.items():
            where = {'$and': [{'scope': json.dumps([cluster, namespace])},
                              {'first': {'$gte': last - groups[indices[0]]['_retrieval_window']}},
                              {'last': {'$lte': first + groups[indices[0]]['_retrieval_window']}}]}
            # Identical vectors under identical filters have the same ANN
            # neighbors. Query once and fan out, retaining each group's identity.
            equivalent = defaultdict(list)
            for i in indices:
                equivalent[vectors[i].tobytes()].append(i)
            representatives = [members[0] for members in equivalent.values()]
            scope_indices = scopes[(cluster, namespace)]
            # HNSW may miss isolated semantic neighbors among many duplicate
            # vectors. Small scopes can be searched exactly at bounded cost.
            if len(scope_indices) <= 512:
                window = groups[indices[0]]['_retrieval_window']
                eligible = [j for j in scope_indices if groups[j]['first_seen'] >= last-window
                            and groups[j]['last_seen'] <= first+window]
                for representative in representatives:
                    scores = vectors[eligible] @ vectors[representative]
                    matches = sorted(zip(eligible, scores), key=lambda hit: (-hit[1], hit[0]))[:limit+1]
                    for i in equivalent[vectors[representative].tobytes()]:
                        pools[i].update([j for j, _ in matches if j != i][:limit])
                        queries += 1
                    exact_queries += 1
                continue
            for start in range(0, len(representatives), 128):
                batch = representatives[start:start+128]
                response = collection.query(query_embeddings=vectors[batch].tolist(), n_results=min(len(groups), limit+1),
                                            where=where, include=['distances'])
                if len(response['ids']) != len(batch):
                    raise ValueError('incomplete ANN response')
                for representative, matches in zip(batch, response['ids']):
                    for i in equivalent[vectors[representative].tobytes()]:
                        pools[i].update([int(j) for j in matches if int(j) != i][:limit])
                        queries += 1
                vector_queries += len(batch)
        info.update(search_mode='hnsw_and_lexical', queried_groups=queries,
                    ann_query_vectors=vector_queries, exact_query_vectors=exact_queries,
                    neighbor_pool_limit=64+limit)
    finally:
        client.delete_collection(name)
    return pools, info
