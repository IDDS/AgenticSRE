"""Remote controlled ONNX thread experiment; retain vectors for equivalence."""
import argparse
import json
from pathlib import Path
import time

import numpy as np
import onnxruntime as ort
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--threads',type=int,choices=[0,2],required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    root = Path('/app/models/all-MiniLM-L6-v2')
    ONNXMiniLM_L6_V2.DOWNLOAD_PATH = root
    encoder = ONNXMiniLM_L6_V2(preferred_providers=['CPUExecutionProvider'])
    started = time.monotonic()
    if args.threads:
        options = ort.SessionOptions()
        options.intra_op_num_threads = args.threads
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        encoder.model = ort.InferenceSession(str(root/'onnx/model.onnx'),sess_options=options,
                                            providers=['CPUExecutionProvider'])
    model = encoder.model
    init = time.monotonic()-started
    texts = [('Database connection reset by peer' if i%2 else '数据库连接被对端重置')+f' shard={i}' for i in range(128)]
    started = time.monotonic()
    vectors = np.asarray(encoder(texts))
    seconds = time.monotonic()-started
    np.save(args.output.with_suffix('.npy'),vectors)
    report = {'threads':args.threads,'session_intra_threads':model.get_session_options().intra_op_num_threads,
        'session_inter_threads':model.get_session_options().inter_op_num_threads,
        'initialization_seconds':round(init,3),'encode_seconds':round(seconds,3),
        'records':len(texts),'shape':list(vectors.shape),
        'process_threads':next(line for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('Threads:'))}
    args.output.write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)


if __name__ == '__main__':
    main()
