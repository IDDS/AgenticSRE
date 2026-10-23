#!/usr/bin/env python3
"""Remote controlled replay: grouping labels and actual neural retrieval."""
import json
import time
from collections import Counter
from agents.alert_compression import compress_alerts
from agents.alert_semantics import recall_candidates, local_embeddings, build_scag, entity_ref


def main():
    rows, truth = [], []
    for incident, text in enumerate(["HTTP 500 request_id={i}", "HTTP 200 request_id={i}",
                                      "version 1 failed request_id={i}", "version 2 failed request_id={i}"]):
        for i in range(100):
            rows.append(dict(name="Log", source="logs", cluster_id="c1", namespace="shop", pod="api",
                             timestamp=1000+i, message=text.format(i=i), severity="warning"))
            truth.append(incident)
    rows.append(dict(rows[0], message="out of memory", severity="critical"))
    truth.append("rare-critical")
    result = compress_alerts(rows)
    choose2 = lambda n: n*(n-1)//2
    predicted = sum(choose2(g["alert_count"]) for g in result["groups"])
    tp = sum(sum(choose2(n) for n in Counter(truth[i] for i in g["alert_indices"]).values()) for g in result["groups"])
    actual = sum(choose2(n) for n in Counter(truth).values())
    # Different words, different entities: actual model must find the paraphrase.
    phrases = ["The database connection was refused", "Unable to connect to database server",
               "Daily backup completed successfully", "User password changed successfully"]
    semantic = compress_alerts([dict(rows[0], pod=f"p{i}", message=t) for i, t in enumerate(phrases)])
    started = time.monotonic()
    vectors = local_embeddings(phrases)
    recall_candidates(semantic)
    pair = {g["group_id"] for g in semantic["groups"][:2]}
    hit = any({p["left"], p["right"]} == pair for p in semantic["candidate_pairs"])
    unrelated = {g["group_id"] for g in semantic["groups"][2:]}
    false_cross = sum(bool({p["left"], p["right"]} & pair) and bool({p["left"], p["right"]} & unrelated) for p in semantic["candidate_pairs"])
    build_scag(semantic)
    report = dict(dataset="controlled fixture, not production accuracy", total=len(rows), groups=len(result["groups"]),
                  compression_ratio=result["compression_ratio"], precision=tp/predicted, recall=tp/actual,
                  false_merge_pairs=predicted-tp, omitted=result["omitted_alerts"],
                  critical_retained=sum(r["severity"] == "critical" for r in result["raw_alerts"]),
                  embedding_dimension=len(vectors[0]),
                  embedding_channels=semantic['retrieval']['embedding_channels'],
                  retrieval=semantic["retrieval"],
                  paraphrase_hit=hit, unrelated_cross_pairs=false_cross,
                  neural_elapsed_s=round(time.monotonic()-started, 3),
                  unsupported_edges_promoted=sum(e["status"] == "evidence_supported" for e in semantic["scag"]["edges"]))
    print(json.dumps(report, indent=2))
    assert tp == predicted == actual and report["omitted"] == 0
    assert report["critical_retained"] == 1 and hit and false_cross == 0
    assert report["embedding_dimension"] == 384 * len(report['embedding_channels'])
    assert semantic["retrieval"]["status"] == "ok"
    assert report["unsupported_edges_promoted"] == 0


if __name__ == "__main__":
    main()
