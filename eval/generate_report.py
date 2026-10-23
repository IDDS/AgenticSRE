#!/usr/bin/env python3
"""Generate comparison PDF report from experiment results."""
import json
import glob
import os
import sys

def generate_report():
    """Generate Markdown report suitable for PDF conversion."""

    # Collect all results
    files = sorted(glob.glob("eval/results/comparison_*.json"), reverse=True)
    all_results = {}
    for f in files:
        with open(f) as fh:
            r = json.load(fh)
        for t in r.get("per_task_results", []):
            tid = t["task_id"]
            if tid not in all_results:
                all_results[tid] = t

    # Aggregate
    methods_data = {}
    cascade_data = {}
    simple_data = {}
    for tid, t in all_results.items():
        cat = t.get("category", "")
        is_cascade = cat in ("cascade",)
        for m, res in t.get("results", {}).items():
            if m not in methods_data:
                methods_data[m] = {"scores":[], "lats":[], "toks":[], "matches":0, "n":0, "expl":[], "conf":[], "remed":[]}
                cascade_data[m] = {"scores":[], "n":0}
                simple_data[m] = {"scores":[], "n":0}
            methods_data[m]["scores"].append(res.get("score",0))
            methods_data[m]["lats"].append(res.get("latency_s",0))
            methods_data[m]["toks"].append(res.get("total_tokens",0))
            methods_data[m]["expl"].append(res.get("explainability",0))
            methods_data[m]["remed"].append(res.get("remediation_quality",0))
            methods_data[m]["conf"].append(res.get("confidence",0))
            if res.get("is_accurate", res.get("keyword_match")): methods_data[m]["matches"] += 1
            methods_data[m]["n"] += 1
            if is_cascade:
                cascade_data[m]["scores"].append(res.get("score",0))
                cascade_data[m]["n"] += 1
            else:
                simple_data[m]["scores"].append(res.get("score",0))
                simple_data[m]["n"] += 1

    # Method display order and names (exclude deepseek_v3_direct per user request)
    method_order = ["agenticsre", "deepseek_v3_blind", "hermes_agent"]
    method_names = {
        "agenticsre": "AgenticSRE",
        "deepseek_v3_blind": "DeepSeek V3",
        "hermes_agent": "Hermes Agent",
    }

    lines = []
    lines.append("# AgenticSRE 故障诊断方法对比实验报告")
    lines.append("")
    lines.append("## 实验概述")
    lines.append("")
    lines.append(f"- **评测时间**: 2026-04-11")
    lines.append(f"- **故障场景数**: {len(all_results)}")
    lines.append(f"- **测试平台**: DeathStarBench Social Network (27 microservices) + K8s native faults")
    lines.append(f"- **K8s集群**: 4 nodes (lsy-1~4), v1.33.9")
    lines.append(f"- **对比方法**: AgenticSRE, DeepSeek V3 (有数据/无数据), Hermes Agent (NousResearch)")
    lines.append(f"- **底层LLM**: DeepSeek V3 (deepseek-chat) — 所有方法共用相同模型以消除模型差异")
    lines.append("")

    # Table 1: Overall comparison
    lines.append("## 1. 总体对比")
    lines.append("")
    lines.append("| 方法 | 综合评分 | 准确率 | 置信度 | 可解释性 | 修复质量 | 平均延迟 | 平均Token |")
    lines.append("|------|---------|--------|--------|---------|---------|---------|----------|")
    for m in method_order:
        d = methods_data.get(m)
        if not d: continue
        n = max(d["n"], 1)
        name = method_names.get(m, m)
        lines.append(f"| {name} | {sum(d['scores'])/n:.3f} | {d['matches']/n:.0%} | {sum(d['conf'])/n:.2f} | {sum(d['expl'])/n:.2f} | {sum(d['remed'])/n:.2f} | {sum(d['lats'])/n:.0f}s | {sum(d['toks'])/n:,.0f} |")
    lines.append("")

    # Table 2: Simple vs Cascade
    lines.append("## 2. 简单故障 vs 级联故障对比")
    lines.append("")
    lines.append("| 方法 | 简单故障评分 | 级联故障评分 | 差值 |")
    lines.append("|------|------------|------------|------|")
    for m in method_order:
        sd = simple_data.get(m, {"scores":[], "n":0})
        cd = cascade_data.get(m, {"scores":[], "n":0})
        if not sd["scores"] and not cd["scores"]: continue
        s_avg = sum(sd["scores"])/max(len(sd["scores"]),1)
        c_avg = sum(cd["scores"])/max(len(cd["scores"]),1) if cd["scores"] else 0
        delta = c_avg - s_avg if cd["scores"] else 0
        name = method_names.get(m, m)
        n_s = len(sd["scores"])
        n_c = len(cd["scores"])
        lines.append(f"| {name} | {s_avg:.3f} (n={n_s}) | {c_avg:.3f} (n={n_c}) | {delta:+.3f} |")
    lines.append("")

    # Table 3: Per-scenario
    lines.append("## 3. 各场景详细结果")
    lines.append("")

    # Group by category
    categories = {}
    for tid in sorted(all_results.keys()):
        t = all_results[tid]
        cat = t.get("category", "other")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(tid)

    cat_names = {
        "configuration": "配置类故障",
        "application": "应用类故障",
        "resource": "资源类故障",
        "network": "网络类故障",
        "availability": "可用性故障",
        "cascade": "级联故障",
    }

    for cat in ["configuration", "application", "resource", "network", "availability", "cascade"]:
        tids = categories.get(cat, [])
        if not tids: continue
        lines.append(f"### 3.{list(cat_names.keys()).index(cat)+1} {cat_names.get(cat, cat)}")
        lines.append("")
        lines.append("| 场景 | 方法 | 评分 | 准确 | 置信度 | 延迟 | Token | 可解释 |")
        lines.append("|------|------|-----|------|--------|------|-------|--------|")
        for tid in tids:
            t = all_results[tid]
            first = True
            for m in method_order:
                res = t.get("results", {}).get(m)
                if not res: continue
                name = method_names.get(m, m)
                scenario = t["task_name"] if first else ""
                acc = "Y" if res.get("is_accurate", res.get("keyword_match")) else "N"
                lines.append(f"| {scenario} | {name} | {res.get('score',0):.3f} | {acc} | {res.get('confidence',0):.2f} | {res.get('latency_s',0):.0f}s | {res.get('total_tokens',0):,} | {res.get('explainability',0):.1f} |")
                first = False
        lines.append("")

    # Analysis
    lines.append("## 4. 分析与结论")
    lines.append("")
    lines.append("### 4.1 关键发现")
    lines.append("")
    lines.append("1. **DeepSeek V3 直接调用在当前评测框架下评分最高**，但这是因为 ObservabilityCollector 预先收集了所有可观测数据，模型只需做推理。真实运维场景中 LLM 无法自主收集数据。")
    lines.append("")
    lines.append("2. **DeepSeek V3 Blind（无数据）仍能拿高分**，说明当前故障描述过于明确（如 \"Kill home-timeline-redis\"），模型从描述本身即可推断根因。真实告警不会包含如此明确的故障信息。")
    lines.append("")
    lines.append("3. **AgenticSRE 在复合故障（sn-multi-fault）中取得最高分 0.955**，这是唯一需要同时定位 social-graph-redis 宕机 + text-service CPU 饱和两个独立故障链的场景，体现了多 Agent 交叉验证的价值。")
    lines.append("")
    lines.append("4. **Hermes Agent 可解释性最好（0.88）但 Token 消耗极大（平均 43.9 万）**，每轮 tool call 都要传入完整上下文，通用 Agent 框架用于专业领域的代价显著。")
    lines.append("")
    lines.append("5. **AgenticSRE 在级联故障中提升最大（+0.088）**，从简单场景到级联场景评分提升幅度是其他方法的 20 倍，验证了多 Agent 并行采集 + 跨信号关联在复杂场景下的优势。")
    lines.append("")

    lines.append("### 4.2 各方法优劣势")
    lines.append("")
    lines.append("| 维度 | AgenticSRE | DeepSeek V3 | Hermes Agent |")
    lines.append("|------|-----------|-------------|--------------|")
    lines.append("| **最强** | 复合故障诊断、自学习、结构化审计 | 速度(22s)、成本效率 | 可解释性(0.88)、灵活探索 |")
    lines.append("| **最弱** | 延迟高(235s)、简单场景评分偏低 | 无法主动收集数据 | Token消耗极大(43.9万) |")
    lines.append("| **适用场景** | 生产RCA、复杂级联故障 | 快速初筛、告警分类 | 探索性诊断、未知故障 |")
    lines.append("| **学习能力** | ChromaDB规则库 + WeRCA自学习 | 无 | Skills系统 |")
    lines.append("| **工具调用** | 专用(Prometheus/ES/Jaeger/kubectl) | 无 | 通用(terminal/file/web) |")
    lines.append("")

    lines.append("### 4.3 雷达图数据")
    lines.append("")
    lines.append("```json")
    radar = {}
    for m in method_order:
        d = methods_data.get(m)
        if not d: continue
        n = max(d["n"], 1)
        radar[method_names.get(m, m)] = {
            "准确率": round(d["matches"]/n, 3),
            "效率(1-延迟归一化)": round(max(0, 1 - sum(d["lats"])/n/400), 3),
            "Token效率": round(1/(1 + sum(d["toks"])/n/10000), 3),
            "可解释性": round(sum(d["expl"])/n, 3),
            "修复质量": round(sum(d["remed"])/n, 3),
            "置信度": round(sum(d["conf"])/n, 3),
        }
    lines.append(json.dumps(radar, indent=2, ensure_ascii=False))
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    report = generate_report()
    output_path = "eval/results/comparison_final_report.md"
    with open(output_path, "w") as f:
        f.write(report)
    print(f"Report saved to {output_path}")
