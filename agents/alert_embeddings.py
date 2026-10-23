"""Offline alert encoders; embedding similarity only retrieves hypotheses."""
from collections import OrderedDict
import hashlib
import os
from pathlib import Path
import threading

LEGACY = 'all-MiniLM-L6-v2'
MULTILINGUAL = 'paraphrase-multilingual-MiniLM-L12-v2'
HYBRID = 'hybrid-minilm'
REVISION = 'ae06c001a2546bef168b9bf8f570ccb1a16aaa27'
HASHES = {
    'onnx/model.onnx': '10f7a088420252b26caf819236ca2c9d2987afd0fc06fec7553b542a5655a05a',
    'tokenizer.json': '2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8',
}


def _cpu_session(path):
    """Bound ONNX workers: automatic host-wide pools oversubscribe CPU quotas."""
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(str(path), sess_options=options, providers=['CPUExecutionProvider'])


class MultilingualEncoder:
    """Pinned 384-dimensional model, 128 tokens, attention-masked mean pooling."""

    def __init__(self, root):
        from tokenizers import Tokenizer
        root = Path(root)
        for name, expected in HASHES.items():
            with (root / name).open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    raise ValueError('offline multilingual model checksum mismatch')
        self.tokenizer = Tokenizer.from_file(str(root / 'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=128)
        self.tokenizer.enable_padding(pad_id=self.tokenizer.token_to_id('<pad>'), pad_token='<pad>')
        self.session = _cpu_session(root / 'onnx/model.onnx')
        self.inputs = {item.name for item in self.session.get_inputs()}

    def __call__(self, texts):
        import numpy as np
        result = []
        for start in range(0, len(texts), 16):
            encoded = self.tokenizer.encode_batch(texts[start:start+16])
            feed = {'input_ids': np.asarray([t.ids for t in encoded], dtype=np.int64),
                    'attention_mask': np.asarray([t.attention_mask for t in encoded], dtype=np.int64),
                    'token_type_ids': np.asarray([t.type_ids for t in encoded], dtype=np.int64)}
            hidden = self.session.run(None, {k:v for k,v in feed.items() if k in self.inputs})[0]
            mask = feed['attention_mask'][..., None]
            vectors = (hidden * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
            vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
            result.extend(vectors.tolist())
        return result


_lock = threading.Lock()
_identity = None
_encoders = []
_cache = OrderedDict()


def embedding_metadata():
    profile = os.environ.get('AGENTICSRE_ALERT_EMBEDDING_MODEL', LEGACY)
    channels = [LEGACY, MULTILINGUAL] if profile == HYBRID else [profile]
    return {'encoder': profile, 'embedding_channels': channels,
            'embedding_revision': REVISION if MULTILINGUAL in channels else None}


def local_embeddings(texts):
    """Cache belongs to a model/path identity; hybrid channels stay separate.

    Hybrid rows concatenate two vectors for transport only. Retrieval must take
    the maximum of their separate cosine scores, never a joint-space cosine.
    Deployment model files are immutable; restart workers to change file content.
    """
    global _identity, _encoders
    meta = embedding_metadata()
    profile = meta['encoder']
    if profile not in (LEGACY, MULTILINGUAL, HYBRID):
        raise ValueError('unsupported offline alert embedding model')
    roots = [(name, str(Path(os.environ.get(
        'AGENTICSRE_ALERT_MULTILINGUAL_PATH' if name == MULTILINGUAL else 'AGENTICSRE_ALERT_EMBEDDING_PATH',
        '/app/models/' + name)).resolve())) for name in meta['embedding_channels']]
    identity = tuple(roots)
    with _lock:
        if identity != _identity:
            # Discard the previous space before loading; a failed switch must
            # not accidentally serve cached vectors from the previous model.
            _identity, _encoders = None, []
            _cache.clear()
            for name, root in roots:
                if name == MULTILINGUAL:
                    _encoders.append(MultilingualEncoder(root))
                else:
                    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
                    required = ('model.onnx', 'tokenizer.json', 'config.json',
                                'special_tokens_map.json', 'tokenizer_config.json', 'vocab.txt')
                    if not all((Path(root) / 'onnx' / name).is_file() for name in required):
                        raise FileNotFoundError('offline embedding model is not installed')
                    ONNXMiniLM_L6_V2.DOWNLOAD_PATH = Path(root)
                    encoder = ONNXMiniLM_L6_V2(preferred_providers=['CPUExecutionProvider'])
                    # Chroma exposes its session as a cached_property. Supply
                    # a bounded session while retaining its tokenizer/pooling.
                    encoder.model = _cpu_session(Path(root) / 'onnx/model.onnx')
                    _encoders.append(encoder)
            _identity = identity
        missing = list(dict.fromkeys(t for t in texts if t not in _cache))
        if missing:
            batches = [encoder(missing) for encoder in _encoders]
            if any(len(batch) != len(missing) for batch in batches):
                raise ValueError('incomplete embedding batch')
            for i, text in enumerate(missing):
                _cache[text] = tuple(float(v) for batch in batches for v in batch[i])
        result = [_cache[t] for t in texts]
        for text in texts:
            _cache.move_to_end(text)
        while len(_cache) > 1024:
            _cache.popitem(last=False)
        return result
