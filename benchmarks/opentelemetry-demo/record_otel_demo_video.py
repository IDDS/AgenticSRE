#!/usr/bin/env python3
"""Record an AgenticSRE + OpenTelemetry Demo end-to-end demo video.

The script records the browser with Playwright while triggering three validated
fault demos on the remote host:
- Network partition -> NetworkChaos cleanup.
- CPU stress -> stress pod cleanup.
- ImagePullBackOff -> deployment rollback.

It overlays narration text and recent scenario logs into the recorded page.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from playwright.sync_api import Page, sync_playwright


BASE_URL = os.environ.get("AGENTICSRE_UI_URL", "http://127.0.0.1:18080").rstrip("/")
USERNAME = os.environ.get("AGENTICSRE_DEMO_USERNAME", "admin")
PASSWORD = os.environ.get("AGENTICSRE_DEMO_PASSWORD", "")
AUTH_TOKEN = os.environ.get("AGENTICSRE_AUTH_TOKEN", "").strip()
REMOTE_HOST = os.environ.get("AGENTICSRE_REMOTE_HOST", "snail")
REMOTE_DEMO_DIR = os.environ.get("AGENTICSRE_REMOTE_OTEL_DIR", "/tmp/agenticsre-otel-demo")
OUT_DIR = Path(os.environ.get("AGENTICSRE_DEMO_OUT_DIR", "demo_recordings"))
ROOT_DIR = Path(__file__).resolve().parents[2]
VENDOR_DIR = ROOT_DIR / "web_app" / "static" / "vendor"
VIDEO_WIDTH = int(os.environ.get("AGENTICSRE_DEMO_VIDEO_WIDTH", "1920"))
VIDEO_HEIGHT = int(os.environ.get("AGENTICSRE_DEMO_VIDEO_HEIGHT", "1080"))
VIDEO_CRF = os.environ.get("AGENTICSRE_DEMO_VIDEO_CRF", "12")
DEMO_METRIC_RANGE = os.environ.get("AGENTICSRE_DEMO_METRIC_RANGE", "10m")
SCENARIO_COOLDOWN_SECONDS = int(os.environ.get("AGENTICSRE_DEMO_SCENARIO_COOLDOWN_SECONDS", "300"))
SCENARIO_TIMEOUT_SECONDS = int(os.environ.get("AGENTICSRE_DEMO_SCENARIO_TIMEOUT_SECONDS", "1500"))
LOCAL_RECOVERY_OBSERVE_SECONDS = int(os.environ.get("AGENTICSRE_DEMO_LOCAL_RECOVERY_OBSERVE_SECONDS", "60"))
REMOTE_SCENARIO_START_RETRIES = int(os.environ.get("AGENTICSRE_DEMO_REMOTE_START_RETRIES", "3"))
DIAGNOSIS_RESULT_HOLD_MS = int(os.environ.get("AGENTICSRE_DEMO_DIAGNOSIS_RESULT_HOLD_SECONDS", "12")) * 1000
HEAL_PLAN_HOLD_MS = int(os.environ.get("AGENTICSRE_DEMO_HEAL_PLAN_HOLD_SECONDS", "12")) * 1000
HEAL_EXECUTED_HOLD_MS = int(os.environ.get("AGENTICSRE_DEMO_HEAL_EXECUTED_HOLD_SECONDS", "12")) * 1000


SCENARIOS = [
    {
        "id": "network",
        "title": "场景一：网络分区故障",
        "env": "AGENTICSRE_RUN_NETWORK_DEMO",
        "script": "run_network_policy_demo.py",
        "query": "OpenTelemetry Demo frontend-proxy 到 frontend 出现网络分区，frontend proxy 返回 503，namespace=otel-demo",
        "payload": {
            "source": "otel-demo-network-demo",
            "namespace": "otel-demo",
            "fault_type": "NetworkPolicyDeny",
            "service": "frontend",
            "deployment": "frontend",
            "network_chaos": "agenticsre-demo-frontend-partition",
            "message": "ChaosMesh NetworkChaos agenticsre-demo-frontend-partition partitions frontend-proxy and frontend; frontend proxy returns 503",
            "diagnosis": {
                "fault_type": "NetworkPolicyDeny",
                "root_cause": "Temporary agenticsre-demo ChaosMesh NetworkChaos/agenticsre-demo-frontend-partition partitions frontend-proxy from frontend",
                "root_cause_component": "frontend",
                "network_chaos": "agenticsre-demo-frontend-partition",
                "affected_services": ["frontend", "frontend-proxy"],
                "remediation_suggestion": "delete temporary NetworkChaos/agenticsre-demo-frontend-partition after approval and verify frontend HTTP recovery",
                "confidence": 0.97,
                "evidence_summary": {
                    "network_chaos": "agenticsre-demo-frontend-partition",
                    "symptom": "frontend proxy changes from HTTP 200 to 503 after ChaosMesh partition injection",
                    "background_load": "otel-demo load-generator is continuously sending requests",
                },
                "timeline": [
                    {"time": "baseline", "event": "frontend returns HTTP 200 under background load"},
                    {"time": "injection", "event": "ChaosMesh NetworkChaos partitions frontend-proxy and frontend"},
                    {"time": "healing", "event": "AgenticSRE removes temporary NetworkChaos and verifies recovery"},
                ],
            },
        },
        "narration": [
            "我们先录制网络类型故障。背景负载持续访问 OpenTelemetry Demo 的 frontend。",
            "故障注入后，业务请求会出现失败或延迟升高，指标监控和告警中心会体现异常。",
            "AgenticSRE 会基于诊断结果检索网络故障自愈策略，执行清理 NetworkChaos 的恢复动作。",
            "恢复后，frontend 请求状态回到正常，错误率下降，闭环完成。",
        ],
    },
    {
        "id": "cpu",
        "title": "场景二：CPU 压力故障",
        "env": "AGENTICSRE_RUN_CPU_DEMO",
        "script": "run_cpu_stress_demo.py",
        "query": "OpenTelemetry Demo namespace 出现临时 CPU stress Pod，容器 CPU 使用率升高，namespace=otel-demo",
        "payload": {
            "source": "otel-demo-cpu-demo",
            "namespace": "otel-demo",
            "fault_type": "HighCPUUsage",
            "pod": "agenticsre-demo-cpu-stress",
            "service": "frontend",
            "deployment": "frontend",
            "message": "Temporary agenticsre-demo CPU stress pod agenticsre-demo-cpu-stress is consuming CPU in otel-demo",
            "diagnosis": {
                "fault_type": "HighCPUUsage",
                "root_cause": "Temporary agenticsre-demo CPU stress pod/agenticsre-demo-cpu-stress is consuming CPU and raising namespace CPU usage",
                "root_cause_component": "agenticsre-demo-cpu-stress",
                "affected_services": ["frontend", "frontend"],
                "remediation_suggestion": "delete temporary CPU stress pod/agenticsre-demo-cpu-stress after approval and verify CPU pressure decreases",
                "confidence": 0.96,
                "evidence_summary": {
                    "stress_pod": "agenticsre-demo-cpu-stress",
                    "metric": "container_cpu_usage_seconds_total shows CPU usage for the stress pod",
                    "background_load": "otel-demo load-generator is continuously sending requests",
                },
                "timeline": [
                    {"time": "baseline", "event": "frontend returns HTTP 200 under background load"},
                    {"time": "injection", "event": "agenticsre-demo-cpu-stress pod starts consuming CPU"},
                    {"time": "healing", "event": "AgenticSRE deletes the temporary stress pod and verifies recovery"},
                ],
            },
        },
        "narration": [
            "第二个场景是资源压力。系统会创建临时 CPU stress Pod，模拟业务运行时资源争用。",
            "在指标监控页面可以观察 CPU 水位和业务延迟变化，告警中心会出现资源压力相关信号。",
            "诊断结果会指向临时压力源，自愈动作是删除 stress Pod 并验证资源水位回落。",
            "恢复验证通过后，自愈中心会记录执行结果和恢复证据。",
        ],
    },
    {
        "id": "image",
        "title": "场景三：镜像拉取失败",
        "env": "AGENTICSRE_RUN_IMAGE_DEMO",
        "script": "run_image_pull_demo.py",
        "query": "OpenTelemetry Demo frontend 发布到不存在镜像标签，新 Pod 进入 ImagePullBackOff，namespace=otel-demo",
        "payload": {
            "source": "otel-demo-image-pull-demo",
            "namespace": "otel-demo",
            "fault_type": "ImagePullError",
            "pod": "frontend-imagepull-demo",
            "service": "frontend",
            "deployment": "frontend",
            "message": "OpenTelemetry Demo frontend rollout created a pod with ImagePullBackOff for a missing image tag",
            "diagnosis": {
                "fault_type": "ImagePullError",
                "root_cause": "Deployment/frontend was updated to a non-existing image tag, causing ImagePullBackOff",
                "root_cause_component": "frontend",
                "affected_services": ["frontend", "frontend"],
                "remediation_suggestion": "rollout undo deployment/frontend to restore the previous working image",
                "confidence": 0.97,
                "evidence_summary": {
                    "deployment": "frontend",
                    "reason": "ImagePullBackOff or ErrImagePull",
                    "safe_recovery": "rollout undo restores the previous ReplicaSet and image",
                },
                "timeline": [
                    {"time": "baseline", "event": "frontend is Ready and serving traffic"},
                    {"time": "injection", "event": "deployment/frontend is patched to a missing image tag"},
                    {"time": "healing", "event": "AgenticSRE executes rollout undo and verifies Ready pods"},
                ],
            },
        },
        "narration": [
            "第三个场景是镜像启动失败。系统会把 frontend 发布到一个不存在的镜像标签。",
            "Kubernetes 事件和 Pod 状态会出现 ImagePullBackOff，这是非常典型的发布类故障。",
            "AgenticSRE 根据诊断结果生成回滚方案，执行 rollout undo 恢复到稳定镜像。",
            "Pod 重新 Ready 后，业务恢复，报告和自愈记录可用于演示归档。",
        ],
    },
]


def write_narration(out_dir: Path) -> Path:
    path = out_dir / "otel_demo_narration.md"
    lines = [
        "# AgenticSRE 运维 OpenTelemetry Demo 录制解说词",
        "",
        "## 开场",
        "本 Demo 展示 AgenticSRE 对 OpenTelemetry Demo 业务系统的闭环运维能力。流程包括背景负载、可观测指标、故障注入、告警检测、根因诊断、自愈执行和恢复验证。",
        "",
    ]
    for item in SCENARIOS:
        lines.append(f"## {item['title']}")
        lines.extend(f"- {text}" for text in item["narration"])
        lines.append("")
    lines.append("## 收尾")
    lines.append("三个场景覆盖网络、资源和发布三类典型故障，均通过 AgenticSRE 完成检测、诊断、自愈和恢复验证。")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def install_overlay(page: Page) -> None:
    page.evaluate(
        """
        () => {
          if (document.getElementById('demo-overlay')) return;
          const style = document.createElement('style');
          style.textContent = `
            #demo-overlay {
              position: fixed; right: 30px; top: 92px; width: 620px; z-index: 9999;
              display: grid; grid-template-columns: 1fr; gap: 8px;
              pointer-events: none; font-family: Inter, system-ui, sans-serif;
            }
            .demo-caption, .demo-log {
              background: rgba(15, 23, 42, 0.86); color: #fff;
              border: 1px solid rgba(255,255,255,0.16); border-radius: 8px;
              box-shadow: 0 16px 42px rgba(15,23,42,0.28);
            }
            .demo-caption { padding: 9px 12px; }
            .demo-title { font-size: 16px; font-weight: 800; color: #93c5fd; margin-bottom: 5px; }
            .demo-text { font-size: 18px; line-height: 1.42; font-weight: 700; }
            .demo-log { padding: 9px 11px; max-height: 96px; overflow: hidden; white-space: pre-wrap; font-size: 12px; line-height: 1.3; color: #dbeafe; }
            .demo-log strong { color: #fbbf24; }
            .demo-metric-panel {
              position: fixed; right: 30px; bottom: 28px; z-index: 9999;
              min-width: 360px; max-width: 520px; pointer-events: none;
              background: rgba(15, 23, 42, 0.88); color: #fff;
              border: 1px solid rgba(255,255,255,0.16); border-radius: 8px;
              box-shadow: 0 16px 42px rgba(15,23,42,0.28);
              padding: 12px 14px; display: none;
            }
            .demo-metric-panel h4 { margin: 0 0 8px; font-size: 14px; color: #93c5fd; }
            .demo-metric-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
            .demo-metric-grid div { background: rgba(255,255,255,0.08); border-radius: 6px; padding: 8px; }
            .demo-metric-grid span { display: block; font-size: 11px; color: #cbd5e1; }
            .demo-metric-grid strong { display: block; font-size: 20px; margin-top: 3px; }
            .demo-timeline-panel {
              position: fixed; left: 282px; bottom: 28px; z-index: 9999;
              width: 470px; pointer-events: none;
              background: rgba(15, 23, 42, 0.88); color: #fff;
              border: 1px solid rgba(255,255,255,0.16); border-radius: 8px;
              box-shadow: 0 16px 42px rgba(15,23,42,0.28);
              padding: 12px 14px; display: none;
            }
            .demo-timeline-panel h4 { margin: 0 0 8px; font-size: 14px; color: #93c5fd; }
            .demo-timeline-window { display: flex; justify-content: space-between; color: #cbd5e1; font-size: 11px; }
            .demo-timeline-note { font-size: 12px; line-height: 1.35; color: #dbeafe; margin-top: 8px; }
          `;
          document.head.appendChild(style);
          const overlay = document.createElement('div');
          overlay.id = 'demo-overlay';
          overlay.innerHTML = `
            <div class="demo-caption">
              <div class="demo-title" id="demo-title">AgenticSRE Demo</div>
              <div class="demo-text" id="demo-caption-text">准备开始录制。</div>
            </div>
            <div class="demo-log" id="demo-log"><strong>运行日志</strong>\\n等待故障实验启动...</div>
          `;
          document.body.appendChild(overlay);
          const metrics = document.createElement('div');
          metrics.id = 'demo-metric-panel';
          metrics.className = 'demo-metric-panel';
          metrics.innerHTML = '<h4>CPU 压力观测</h4><div class="demo-metric-grid"><div><span>stress pod</span><strong id="demo-stress-cpu">-</strong></div><div><span>namespace</span><strong id="demo-ns-cpu">-</strong></div><div><span>frontend</span><strong id="demo-frontend-cpu">-</strong></div></div>';
          document.body.appendChild(metrics);
          const timeline = document.createElement('div');
          timeline.id = 'demo-timeline-panel';
          timeline.className = 'demo-timeline-panel';
          timeline.innerHTML = '<h4>故障过程提示</h4><div class="demo-timeline-window"><span id="demo-window-start">-</span><span>当前 10M 指标窗口</span><span id="demo-window-end">-</span></div><div class="demo-timeline-note" id="demo-timeline-note">等待故障事件...</div>';
          document.body.appendChild(timeline);
        }
        """
    )


def install_resource_routes(page: Page) -> None:
    def handle(route):
        url = route.request.url
        if "fonts.googleapis.com" in url:
            route.fulfill(status=200, content_type="text/css", body="")
            return
        if "fonts.gstatic.com" in url:
            route.abort()
            return
        if "cdn.jsdelivr.net" in url:
            if "chartjs-adapter-date-fns" in url:
                path = VENDOR_DIR / "chartjs-adapter-date-fns.bundle.min.js"
            elif "chartjs-plugin-annotation" in url:
                path = VENDOR_DIR / "chartjs-plugin-annotation.min.js"
            elif "chart.js" in url:
                path = VENDOR_DIR / "chart.umd.js"
            else:
                path = None
            body = path.read_text(encoding="utf-8") if path and path.exists() else ""
            route.fulfill(status=200, content_type="application/javascript", body=body)
            return
        route.continue_()

    page.route("**/*", handle)


def set_caption(page: Page, title: str, text: str) -> None:
    page.evaluate(
        """([title, text]) => {
          const t = document.getElementById('demo-title');
          const c = document.getElementById('demo-caption-text');
          if (t) t.textContent = title;
          if (c) c.textContent = text;
        }""",
        [title, text],
    )


def set_log(page: Page, lines: Iterable[str]) -> None:
    text = "\n".join(list(lines)[-5:])
    page.evaluate(
        """(text) => {
          const el = document.getElementById('demo-log');
          if (el) el.innerHTML = '<strong>运行日志</strong>\\n' + text.replace(/[&<>]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[ch]));
        }""",
        text,
    )


def _fmt_event_ts(ts: float | int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")


def update_timeline_panel(page: Page, markers: dict[str, int], visible: bool, note: str = "") -> None:
    now = int(time.time())
    window_seconds = 600 if DEMO_METRIC_RANGE == "10m" else 900 if DEMO_METRIC_RANGE == "15m" else 1800
    window_start = now - window_seconds
    page.evaluate(
        """([visible, markers, windowStart, windowEnd, note]) => {
          const panel = document.getElementById('demo-timeline-panel');
          if (!panel) return;
          panel.style.display = visible ? 'block' : 'none';
          document.getElementById('demo-window-start').textContent = new Date(windowStart * 1000).toLocaleTimeString('zh-CN', {hour12:false});
          document.getElementById('demo-window-end').textContent = new Date(windowEnd * 1000).toLocaleTimeString('zh-CN', {hour12:false});
          const noteEl = document.getElementById('demo-timeline-note');
          if (noteEl) noteEl.textContent = note || '当前展示最近 10 分钟指标窗口，结合运行日志观察故障注入、诊断、自愈和恢复过程。';
        }""",
        [visible, markers, window_start, now, note],
    )


def update_chart_time_markers(page: Page, markers: dict[str, int], visible: bool) -> None:
    page.evaluate(
        """() => {
          document.querySelectorAll('.demo-chart-time-mark,.demo-chart-time-label').forEach(el => el.remove());
        }""",
    )


def update_cpu_panel(page: Page, lines: Iterable[str], visible: bool) -> None:
    recent = list(lines)
    metrics: dict[str, str] = {}
    for line in reversed(recent):
        if line.strip().startswith('"stress_cpu_cores"') or line.strip().startswith('"frontend_cpu_cores"') or line.strip().startswith('"namespace_cpu_cores"'):
            key, _, raw = line.partition(":")
            metrics[key.strip().strip('"')] = raw.strip().strip('",')
        if len(metrics) >= 3:
            break
    page.evaluate(
        """([visible, metrics]) => {
          const panel = document.getElementById('demo-metric-panel');
          if (!panel) return;
          panel.style.display = visible ? 'block' : 'none';
          const set = (id, key) => {
            const el = document.getElementById(id);
            if (!el) return;
            const v = metrics[key];
            el.textContent = v == null ? '-' : `${Number(v).toFixed(3)} cores`;
          };
          set('demo-stress-cpu', 'stress_cpu_cores');
          set('demo-ns-cpu', 'namespace_cpu_cores');
          set('demo-frontend-cpu', 'frontend_cpu_cores');
        }""",
        [visible, metrics],
    )


def add_frame_point(
    frame_points: list[dict],
    video_started_at: float,
    scenario: str,
    stage: str,
    label: str,
    view: str,
) -> None:
    frame_points.append(
        {
            "scenario": scenario,
            "stage": stage,
            "label": label,
            "view": view,
            "offset_s": round(max(time.time() - video_started_at, 0.0), 1),
            "wall_time": datetime.now().isoformat(timespec="seconds"),
        }
    )


def has_frame_point(frame_points: list[dict], scenario: str, stage: str) -> bool:
    return any(point.get("scenario") == scenario and point.get("stage") == stage for point in frame_points)


def wait_with_frame_point(
    page: Page,
    wait_ms: int,
    frame_points: list[dict],
    video_started_at: float,
    scenario: str,
    stage: str,
    label: str,
    view: str,
    capture_at: float = 0.55,
) -> None:
    first_wait = max(0, min(wait_ms, int(wait_ms * capture_at)))
    second_wait = max(0, wait_ms - first_wait)
    if first_wait:
        page.wait_for_timeout(first_wait)
    add_frame_point(frame_points, video_started_at, scenario, stage, label, view)
    if second_wait:
        page.wait_for_timeout(second_wait)


def extract_frame_checks(video_path: Path, out_dir: Path, frame_points: list[dict]) -> list[dict]:
    frame_dir = out_dir / "frame_checks"
    frame_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[dict] = []
    for index, point in enumerate(frame_points, start=1):
        scenario = re.sub(r"[^A-Za-z0-9_-]+", "_", str(point.get("scenario", "scenario")))
        stage = re.sub(r"[^A-Za-z0-9_-]+", "_", str(point.get("stage", "stage")))
        filename = f"{index:02d}_{scenario}_{stage}.jpg"
        output_path = frame_dir / filename
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{float(point['offset_s']):.1f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(output_path),
        ]
        result = subprocess.run(cmd, text=True, capture_output=True)
        item = {**point, "frame": str(output_path), "returncode": result.returncode}
        if result.returncode != 0:
            item["error"] = (result.stderr or result.stdout or "")[-1200:]
        extracted.append(item)
    (frame_dir / "frame_manifest.json").write_text(
        json.dumps(extracted, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return extracted


def login(page: Page) -> None:
    if not PASSWORD:
        raise RuntimeError("AGENTICSRE_DEMO_PASSWORD is required for this demo")
    page.goto(f"{BASE_URL}/login", wait_until="commit", timeout=30000)
    page.wait_for_selector("#login-username", timeout=15000)
    page.evaluate(
        """async ([username, password]) => {
          const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username, password})
          });
          if (!res.ok) throw new Error(await res.text());
        }""",
        [USERNAME, PASSWORD],
    )
    page.goto(f"{BASE_URL}/", wait_until="commit", timeout=30000)
    page.wait_for_selector(".nav-item[data-view=\"overview\"]", timeout=20000)
    page.wait_for_timeout(1500)
    install_overlay(page)


def set_demo_metric_range(page: Page, range_value: str = DEMO_METRIC_RANGE) -> None:
    page.evaluate(
        """async (rangeValue) => {
          const ns = document.getElementById('metrics-ns');
          if (ns && [...ns.options].some(o => o.value === 'otel-demo')) ns.value = 'otel-demo';
          if (typeof state !== 'undefined') state.metricRange = rangeValue;
          document.querySelectorAll('#metrics-time-range .time-range-btn').forEach((btn) => {
            btn.classList.toggle('active', btn.dataset.range === rangeValue);
          });
          if (typeof loadMetricCharts === 'function') await loadMetricCharts();
        }""",
        range_value,
    )


def switch_view(page: Page, view: str, title: str, caption: str, wait_ms: int = 3500) -> None:
    page.evaluate("""() => { if (typeof closeHealPlanModal === 'function') closeHealPlanModal(); }""")
    set_caption(page, title, caption)
    page.click(f'.nav-item[data-view="{view}"]')
    page.wait_for_timeout(800)
    if view == "metrics":
        try:
            page.select_option("#metrics-ns", "otel-demo")
            set_demo_metric_range(page)
            page.wait_for_timeout(2200)
        except Exception:
            pass
    page.wait_for_timeout(max(wait_ms - 3000, 700))


def show_rca_result(page: Page, spec: dict, wait_ms: int = 16000) -> None:
    """Render a paced multi-agent diagnosis process for the current scenario."""
    switch_view(
        page,
        "rca",
        spec["title"],
        "多智能体开始协作诊断：告警压缩、上下文检索、假设生成、证据调查和质量评估会逐步展开。",
        1500,
    )
    page.evaluate(
        """(spec) => {
          const query = document.getElementById('rca-query');
          const ns = document.getElementById('rca-ns');
          if (query) query.value = spec.query || '';
          if (ns) ns.value = spec.payload?.namespace || 'otel-demo';
          const progress = document.getElementById('rca-progress');
          if (progress) {
            progress.style.display = 'block';
            const phases = document.getElementById('rca-phases');
            if (phases) phases.innerHTML = '';
            const hyp = document.getElementById('rca-hypotheses');
            const hypList = document.getElementById('rca-hyp-list');
            if (hyp && hypList) { hyp.style.display = 'none'; hypList.innerHTML = ''; }
            const ev = document.getElementById('rca-evidence');
            const evGrid = document.getElementById('rca-evidence-grid');
            if (ev && evGrid) { ev.style.display = 'none'; evGrid.innerHTML = ''; }
            const log = document.getElementById('rca-log');
            if (log) log.textContent = '';
          }
          const card = document.getElementById('rca-result-card');
          const content = document.getElementById('rca-result-content');
          if (card) card.style.display = 'none';
          if (content) content.innerHTML = '';
          window.scrollTo({top: 0, behavior: 'instant'});
        }""",
        spec,
    )
    phases = [
        (0, "Metric Agent 查询 OTel 请求量、P99 延迟、5xx 和资源水位。", 1800),
        (1, "Context Agent 读取 namespace、deployment、pod、Chaos/K8s 事件上下文。", 1800),
        (2, "Hypothesis Agent 生成候选根因并排序。", 2200),
        (3, "Event Agent / Log Agent 收集 Kubernetes 事件、Pod 状态和异常证据。", 2600),
        (4, "Graph Agent 关联业务组件、负载、故障对象和影响路径。", 1800),
        (7, "Judge Agent 做质量评估，校验置信度和证据覆盖。", 1800),
        (11, "Closure Agent 输出诊断闭环，并把结果交给自愈策略检索。", 1800),
    ]
    for phase, caption, pause in phases:
        set_caption(page, spec["title"], caption)
        page.evaluate(
            """([phase, spec]) => {
              updatePhase(phase, PHASE_NAMES[phase], 'done');
              const log = document.getElementById('rca-log');
              if (log) {
                const names = {0:'Metric Agent',1:'Context Agent',2:'Hypothesis Agent',3:'Event/Log Agent',4:'Graph Agent',7:'Judge Agent',11:'Closure Agent'};
                log.textContent += `${names[phase] || 'Agent'}: ${PHASE_NAMES[phase]} completed\\n`;
                log.scrollTop = log.scrollHeight;
              }
              if (phase === 2) {
                renderHypotheses([
                  {confidence: spec.payload.diagnosis.confidence || 0.96, description: spec.payload.diagnosis.root_cause},
                  {confidence: 0.42, description: '业务负载或下游依赖导致的暂态抖动'}
                ]);
              }
              if (phase === 3 || phase === 4) {
                const evidence = spec.payload.diagnosis.evidence_summary || {};
                Object.entries(evidence).slice(0, phase === 3 ? 2 : 4).forEach(([k, v]) => addEvidenceCard(k, String(v), true));
              }
            }""",
            [phase, spec],
        )
        page.wait_for_timeout(pause)
    set_caption(page, spec["title"], "根因诊断完成：展示置信度、证据摘要和修复建议，然后进入自愈策略检索。")
    page.evaluate("""(spec) => renderRCAFinalResult({status: 'completed', result: spec.payload.diagnosis})""", spec)
    page.wait_for_timeout(max(wait_ms, DIAGNOSIS_RESULT_HOLD_MS))


def focus_cpu_metrics(
    page: Page,
    title: str,
    caption: str,
    wait_ms: int = 10000,
    markers: dict[str, int] | None = None,
) -> None:
    switch_view(page, "metrics", title, caption, 2500)
    page.evaluate(
        """() => {
          const target = document.getElementById('chart-container-cpu') || document.getElementById('chart-node-cpu');
          if (target) target.scrollIntoView({block: 'center', behavior: 'instant'});
        }"""
    )
    update_chart_time_markers(page, markers or {}, bool(markers))
    page.wait_for_timeout(wait_ms)


def focus_business_metrics(
    page: Page,
    title: str,
    caption: str,
    wait_ms: int = 11000,
    markers: dict[str, int] | None = None,
) -> None:
    switch_view(page, "metrics", title, caption, 2500)
    page.evaluate(
        """() => {
          const target = document.getElementById('chart-biz-apiserver') ||
                         document.getElementById('chart-biz-replicas') ||
                         document.getElementById('chart-biz-restarts');
          if (target) target.scrollIntoView({block: 'center', behavior: 'instant'});
        }"""
    )
    update_chart_time_markers(page, markers or {}, bool(markers))
    page.wait_for_timeout(wait_ms)


def show_heal_plan(page: Page, spec: dict, wait_ms: int = 12000) -> None:
    """Open the real self-healing plan modal and dry-run it for the camera."""
    switch_view(
        page,
        "healcenter",
        spec["title"],
        "自愈方案由诊断结果驱动，并从自愈策略库检索可执行动作；这里展示可编辑方案和 Dry-run 输出。",
        1500,
    )
    page.evaluate(
        """async (payload) => {
          const result = await api('/api/heal/capability', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
            throwOnError: true,
          });
          window.__demoLastHealResult = result;
          window.__demoLastHealPayload = payload;
          openHealPlanModal(result, payload, { source: 'otel-demo-recording' });
          window.scrollTo({top: 0, behavior: 'instant'});
        }""",
        spec["payload"],
    )
    page.wait_for_selector("#heal-plan-modal[style*=\"flex\"]", timeout=15000)
    page.wait_for_timeout(3500)
    page.evaluate("""() => executeHealPlan(true)""")
    page.wait_for_timeout(3000)
    page.evaluate(
        """() => {
          const out = document.getElementById('heal-plan-output');
          if (out) out.scrollIntoView({block: 'center', behavior: 'instant'});
        }"""
    )
    page.wait_for_timeout(max(wait_ms, HEAL_PLAN_HOLD_MS))


def show_latest_heal_record(page: Page, spec: dict, wait_ms: int = 7000) -> None:
    switch_view(
        page,
        "healcenter",
        spec["title"],
        "自愈中心保留真实执行和 Dry-run 记录，包含动作、诊断驱动标记、验证结果和回滚入口。",
        1200,
    )
    page.evaluate(
        """() => {
          closeHealPlanModal();
          const runs = document.getElementById('heal-runs');
          if (runs) runs.scrollIntoView({block: 'center', behavior: 'instant'});
          const first = document.querySelector('#heal-runs .fault-exp-run button');
          if (first) first.click();
        }"""
    )
    page.wait_for_timeout(max(wait_ms, HEAL_EXECUTED_HOLD_MS))


def classify_stage(line: str) -> str | None:
    if line.startswith("AGENTICSRE_STAGE_EVENT "):
        try:
            raw = json.loads(line.partition(" ")[2])
            stage_name = str(raw.get("stage") or "")
        except Exception:
            stage_name = ""
        if stage_name == "baseline_with_background_load":
            return "baseline"
        if stage_name.endswith("fault_injected") or stage_name == "cpu_stress_injected":
            return "injected"
        if stage_name == "self_healing_executed":
            return "healing"
        if stage_name == "recovered":
            return "recovered"
    if '"stage": "baseline_with_background_load"' in line:
        return "baseline"
    if re.search(r'"stage": ".*fault_injected"', line) or '"stage": "cpu_stress_injected"' in line:
        return "injected"
    if '"stage": "diagnosis_and_self_healing_plan"' in line:
        return "diagnosis"
    if '"stage": "self_healing_executed"' in line:
        return "healing"
    if '"stage": "recovered"' in line or '"recovered": true' in line:
        return "recovered"
    return None


def extract_stage_event(line: str) -> tuple[str | None, int | None]:
    stage = classify_stage(line)
    if line.startswith("AGENTICSRE_STAGE_EVENT "):
        try:
            event = json.loads(line.partition(" ")[2])
        except Exception:
            return stage, None
        ts = event.get("event_ts")
        return stage, int(ts) if isinstance(ts, (int, float)) else None
    if not stage or '"event_ts"' not in line:
        return stage, None
    try:
        event = json.loads(line)
    except Exception:
        return stage, None
    ts = event.get("event_ts")
    return stage, int(ts) if isinstance(ts, (int, float)) else None


def stream_process(proc: subprocess.Popen[str], q: queue.Queue[str], log_path: Path) -> None:
    with log_path.open("a", encoding="utf-8") as fh:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            fh.write(line + "\n")
            fh.flush()
            if line:
                q.put(line)


def _run_remote_scenario_once(spec: dict, log_queue: queue.Queue[str], out_dir: Path, attempt: int) -> tuple[int, bool]:
    if not AUTH_TOKEN:
        raise RuntimeError("AGENTICSRE_AUTH_TOKEN is required for this demo")
    env = (
        f"{spec['env']}=1 "
        "AGENTICSRE_BASE_URL=http://127.0.0.1:8080 "
        f"AGENTICSRE_AUTH_TOKEN={AUTH_TOKEN} "
    )
    command = f"cd {REMOTE_DEMO_DIR} && {env} python3 {spec['script']}"
    proc = subprocess.Popen(
        [
            "ssh",
            "-o", "ConnectTimeout=20",
            "-o", "ConnectionAttempts=2",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=6",
            REMOTE_HOST,
            command,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    log_path = out_dir / f"{spec['id']}_scenario.log"
    thread = threading.Thread(target=stream_process, args=(proc, log_queue, log_path), daemon=True)
    thread.start()
    code = proc.wait()
    thread.join(timeout=2)
    text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    started = "AGENTICSRE_STAGE_EVENT" in text
    if code != 0 and not started and attempt < REMOTE_SCENARIO_START_RETRIES:
        message = f"Remote scenario SSH start failed before stage event, retry {attempt}/{REMOTE_SCENARIO_START_RETRIES}..."
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(message + "\n")
        log_queue.put(message)
    return code, started


def drain_log_queue(
    log_queue: queue.Queue[str],
    recent: list[str],
    shown_stages: set[str],
    stages_to_show: list[str],
    stage_markers: dict[str, int],
    *,
    min_wait_ms: int = 800,
    max_wait_ms: int = 3000,
    wait_for: Iterable[str] = (),
    search_from: int | None = None,
) -> None:
    deadline = time.time() + max_wait_ms / 1000
    min_deadline = time.time() + min_wait_ms / 1000
    required = list(wait_for)
    required_start = len(recent) if search_from is None else max(0, search_from)

    while time.time() < deadline:
        drained = False
        while not log_queue.empty():
            drained = True
            line = log_queue.get()
            recent.append(line)
            stage, event_ts = extract_stage_event(line)
            if stage and stage not in shown_stages:
                shown_stages.add(stage)
                stages_to_show.append(stage)
            if event_ts and stage in {"baseline", "injected", "healing", "recovered"}:
                stage_markers[stage] = event_ts
        required_lines = recent[required_start:]
        has_required = all(any(token in line for line in required_lines) for token in required)
        if time.time() >= min_deadline and (has_required or not required):
            break
        if not drained:
            time.sleep(0.2)


def latest_stage_event_index(lines: list[str], stage: str) -> int:
    stage_names = {
        "baseline": ["baseline_with_background_load"],
        "injected": ["network_fault_injected", "cpu_stress_injected", "image_pull_fault_injected"],
        "diagnosis": ["diagnosis_and_self_healing_plan"],
        "healing": ["self_healing_executed"],
        "recovered": ["recovered"],
    }
    names = stage_names.get(stage, [stage])
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if not line.startswith("AGENTICSRE_STAGE_EVENT "):
            continue
        if any(f'"stage": "{name}"' in line for name in names):
            return index
    return len(lines)


def run_remote_scenario(spec: dict, log_queue: queue.Queue[str], out_dir: Path) -> int:
    code = 255
    for attempt in range(1, REMOTE_SCENARIO_START_RETRIES + 1):
        code, started = _run_remote_scenario_once(spec, log_queue, out_dir, attempt)
        if code == 0 or started:
            return code
        time.sleep(min(10 * attempt, 30))
    return code


def record() -> dict:
    started = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUT_DIR / f"otel_demo_{started}"
    video_dir = out_dir / "video_raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)
    narration_path = write_narration(out_dir)
    events = []
    frame_points: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT},
            device_scale_factor=1,
            record_video_dir=str(video_dir),
            record_video_size={"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT},
        )
        page = context.new_page()
        video_started_at = time.time()
        install_resource_routes(page)
        login(page)

        set_caption(page, "开场", "AgenticSRE 已接入 OpenTelemetry Demo。下面展示三类故障的检测、诊断、自愈和恢复验证闭环。")
        switch_view(page, "overview", "开场", "首先确认集群和 OTel Demo 工作负载处于运行状态。", 5000)
        switch_view(page, "metrics", "基线指标", "指标监控使用 10 分钟窗口展示请求量、延迟、错误率以及系统资源曲线，作为故障前基线。", 6500)

        for spec in SCENARIOS:
            if events and SCENARIO_COOLDOWN_SECONDS > 0:
                update_timeline_panel(page, {}, False)
                switch_view(
                    page,
                    "metrics",
                    "场景间隔",
                    f"等待 {SCENARIO_COOLDOWN_SECONDS} 秒，让上一故障影响从 10 分钟指标窗口中滑出，避免不同故障曲线互相干扰。",
                    2500,
                )
                page.wait_for_timeout(SCENARIO_COOLDOWN_SECONDS * 1000)
            q: queue.Queue[str] = queue.Queue()
            stage_markers: dict[str, int] = {}
            set_caption(page, spec["title"], spec["narration"][0])
            switch_view(page, "metrics", spec["title"], "故障开始前先看 OTel Demo 10 分钟基线：背景负载正在产生请求量、延迟和资源曲线。", 5000)
            switch_view(page, "faultlab", spec["title"], "启动故障实验前，展示故障实验入口和可观测目标。", 3500)
            scenario_thread_result: dict[str, int | None] = {"code": None}

            def target() -> None:
                scenario_thread_result["code"] = run_remote_scenario(spec, q, out_dir)

            t = threading.Thread(target=target, daemon=True)
            t.start()

            recent: list[str] = []
            shown_stages: set[str] = set()
            pending_stages: list[str] = []
            scenario_started = time.time()
            while t.is_alive():
                stages_to_show: list[str] = pending_stages
                pending_stages = []
                drain_log_queue(q, recent, shown_stages, stages_to_show, stage_markers, min_wait_ms=0, max_wait_ms=200)
                set_log(page, recent)
                update_cpu_panel(page, recent, spec["id"] == "cpu" and "injected" in shown_stages and "healing" not in shown_stages)
                update_chart_time_markers(page, stage_markers, bool(stage_markers))
                if stages_to_show:
                    current_stages = list(stages_to_show)
                    for stage_to_show in current_stages:
                        stage_event_index = latest_stage_event_index(recent, stage_to_show)
                        wait_tokens = []
                        if stage_to_show == "baseline":
                            wait_tokens = ['"frontend_5xx_per_s"']
                        elif stage_to_show == "injected" and spec["id"] == "cpu":
                            wait_tokens = ['"stress_cpu_cores"', '"namespace_cpu_cores"']
                        elif stage_to_show == "injected":
                            wait_tokens = ['"frontend_5xx_per_s"']
                        elif stage_to_show == "recovered" and spec["id"] == "image":
                            wait_tokens = ['"image_pull_pods"']
                        elif stage_to_show == "recovered":
                            wait_tokens = ['"frontend_5xx_per_s"']
                        if wait_tokens:
                            drain_log_queue(
                                q,
                                recent,
                                shown_stages,
                                stages_to_show,
                                stage_markers,
                                min_wait_ms=1000,
                                max_wait_ms=4500,
                                wait_for=wait_tokens,
                                search_from=stage_event_index,
                            )
                        set_log(page, recent)
                        update_cpu_panel(page, recent, spec["id"] == "cpu" and stage_to_show in {"injected", "diagnosis"})
                        if stage_to_show == "baseline":
                            update_timeline_panel(page, stage_markers, True, "基线已记录，等待故障注入时间点。")
                            update_chart_time_markers(page, stage_markers, True)
                            switch_view(page, "metrics", spec["title"], "基线阶段：背景负载持续访问业务，10 分钟窗口提供清晰参考。", 2500)
                            wait_with_frame_point(
                                page,
                                4000,
                                frame_points,
                                video_started_at,
                                spec["id"],
                                "baseline_metrics",
                                "故障前 10 分钟基线指标",
                                "metrics",
                            )
                        elif stage_to_show == "injected":
                            update_timeline_panel(page, stage_markers, True, "故障已注入，当前聚焦最能体现该故障的指标曲线。")
                            update_chart_time_markers(page, stage_markers, True)
                            if spec["id"] == "cpu":
                                focus_cpu_metrics(
                                    page,
                                    spec["title"],
                                    "CPU stress Pod 已注入：这里聚焦容器 CPU Top10、节点 CPU 和实时 CPU 面板，观察 stress pod 与 namespace CPU 抬升。",
                                    2500,
                                    stage_markers,
                                )
                                wait_with_frame_point(
                                    page,
                                    21500,
                                    frame_points,
                                    video_started_at,
                                    spec["id"],
                                    "injected_cpu_metrics",
                                    "CPU 压力注入后指标峰值",
                                    "metrics",
                                    capture_at=0.7,
                                )
                            else:
                                focus_business_metrics(
                                    page,
                                    spec["title"],
                                    f"{spec['narration'][1]} 当前使用 10 分钟窗口，聚焦请求量、P99 延迟和 5xx 曲线。",
                                    2500,
                                    stage_markers,
                                )
                                wait_with_frame_point(
                                    page,
                                    19500,
                                    frame_points,
                                    video_started_at,
                                    spec["id"],
                                    "injected_business_metrics",
                                    "故障注入后业务指标变化",
                                    "metrics",
                                    capture_at=0.65,
                                )
                            switch_view(page, "alerts", spec["title"], "告警中心用于承接异常信号，并能从告警进入分析和自愈。", 5500)
                            switch_view(page, "events", spec["title"], "事件追踪页展示 Kubernetes/Chaos/ImagePull 等离散证据。", 5000)
                            if spec["id"] == "image":
                                add_frame_point(
                                    frame_points,
                                    video_started_at,
                                    spec["id"],
                                    "injected_k8s_events",
                                    "ImagePullBackOff 事件与 Pod 状态证据",
                                    "events",
                                )
                        elif stage_to_show == "diagnosis":
                            show_rca_result(page, spec, DIAGNOSIS_RESULT_HOLD_MS)
                            wait_with_frame_point(
                                page,
                                3000,
                                frame_points,
                                video_started_at,
                                spec["id"],
                                "diagnosis_agents",
                                "多智能体根因诊断过程与结果",
                                "rca",
                            )
                            show_heal_plan(page, spec, HEAL_PLAN_HOLD_MS)
                            wait_with_frame_point(
                                page,
                                3000,
                                frame_points,
                                video_started_at,
                                spec["id"],
                                "healing_plan",
                                "诊断驱动的可编辑自愈方案",
                                "healcenter",
                            )
                        elif stage_to_show == "healing":
                            update_cpu_panel(page, recent, False)
                            update_timeline_panel(page, stage_markers, True, "自愈动作已执行，等待恢复验证时间点。")
                            update_chart_time_markers(page, stage_markers, True)
                            show_latest_heal_record(page, spec, HEAL_EXECUTED_HOLD_MS)
                            wait_with_frame_point(
                                page,
                                3500,
                                frame_points,
                                video_started_at,
                                spec["id"],
                                "healing_executed",
                                "自愈执行记录与验证结果",
                                "healcenter",
                            )
                        elif stage_to_show == "recovered":
                            update_timeline_panel(page, stage_markers, True, "恢复验证通过，继续观察指标采样窗口中的回落或稳定状态。")
                            update_chart_time_markers(page, stage_markers, True)
                            if spec["id"] == "cpu":
                                focus_cpu_metrics(page, spec["title"], spec["narration"][3], 2500, stage_markers)
                            else:
                                focus_business_metrics(page, spec["title"], spec["narration"][3], 2500, stage_markers)
                            switch_view(
                                page,
                                "metrics",
                                spec["title"],
                                f"恢复验证完成，本地继续观察 {LOCAL_RECOVERY_OBSERVE_SECONDS} 秒，等待 Prometheus 采样窗口显示曲线回落。",
                                2500,
                            )
                            if spec["id"] == "cpu":
                                page.evaluate("""() => document.getElementById('chart-container-cpu')?.scrollIntoView({block:'center', behavior:'instant'})""")
                            else:
                                page.evaluate("""() => document.getElementById('chart-biz-apiserver')?.scrollIntoView({block:'center', behavior:'instant'})""")
                            update_chart_time_markers(page, stage_markers, True)
                            wait_with_frame_point(
                                page,
                                LOCAL_RECOVERY_OBSERVE_SECONDS * 1000,
                                frame_points,
                                video_started_at,
                                spec["id"],
                                "recovery_metrics",
                                "恢复后指标采样窗口",
                                "metrics",
                                capture_at=0.85,
                            )
                    pending_stages.extend(stages_to_show[len(current_stages):])
                else:
                    page.wait_for_timeout(1800)
                if time.time() - scenario_started > SCENARIO_TIMEOUT_SECONDS:
                    raise RuntimeError(f"scenario timeout: {spec['id']}")
            t.join()
            while not q.empty():
                line = q.get()
                recent.append(line)
                stage, event_ts = extract_stage_event(line)
                if event_ts and stage in {"baseline", "injected", "healing", "recovered"}:
                    stage_markers[stage] = event_ts
            set_log(page, recent)
            code = scenario_thread_result["code"]
            events.append({"scenario": spec["id"], "returncode": code, "elapsed_s": round(time.time() - scenario_started, 1)})
            if code != 0:
                set_caption(page, spec["title"], f"场景执行失败，返回码 {code}。请查看日志文件。")
                page.wait_for_timeout(8000)
                raise RuntimeError(f"scenario {spec['id']} failed with return code {code}")
            update_cpu_panel(page, recent, False)
            if "healing" in shown_stages and not has_frame_point(frame_points, spec["id"], "healing_executed"):
                show_latest_heal_record(page, spec, HEAL_EXECUTED_HOLD_MS)
                add_frame_point(
                    frame_points,
                    video_started_at,
                    spec["id"],
                    "healing_executed",
                    "自愈执行记录与验证结果",
                    "healcenter",
                )
            if "recovered" in shown_stages and not has_frame_point(frame_points, spec["id"], "recovery_metrics"):
                update_timeline_panel(page, stage_markers, True, "恢复验证通过，补充观察恢复后指标采样窗口。")
                update_chart_time_markers(page, stage_markers, True)
                if spec["id"] == "cpu":
                    focus_cpu_metrics(page, spec["title"], spec["narration"][3], 2500, stage_markers)
                    page.evaluate("""() => document.getElementById('chart-container-cpu')?.scrollIntoView({block:'center', behavior:'instant'})""")
                else:
                    focus_business_metrics(page, spec["title"], spec["narration"][3], 2500, stage_markers)
                    page.evaluate("""() => document.getElementById('chart-biz-apiserver')?.scrollIntoView({block:'center', behavior:'instant'})""")
                update_chart_time_markers(page, stage_markers, True)
                wait_with_frame_point(
                    page,
                    min(LOCAL_RECOVERY_OBSERVE_SECONDS, 30) * 1000,
                    frame_points,
                    video_started_at,
                    spec["id"],
                    "recovery_metrics",
                    "恢复后指标采样窗口",
                    "metrics",
                    capture_at=0.8,
                )
            update_timeline_panel(page, stage_markers, True, "本场景已完成，可对照曲线高峰、回落和自愈执行记录。")
            update_chart_time_markers(page, stage_markers, True)
            if spec["id"] == "cpu":
                focus_cpu_metrics(page, spec["title"], spec["narration"][3], 14000, stage_markers)
            else:
                focus_business_metrics(page, spec["title"], spec["narration"][3], 14000, stage_markers)
            show_latest_heal_record(page, spec, 7000)

        switch_view(page, "reports", "收尾", "最后进入故障报告页面，RCA 与自愈记录可以导出为报告材料。", 7000)
        set_caption(page, "收尾", "三类故障均完成背景负载、监测、诊断、自愈和恢复验证。Demo 录制结束。")
        page.wait_for_timeout(6500)
        context.close()
        browser.close()

    raw_videos = sorted(video_dir.glob("*.webm"), key=lambda p: p.stat().st_mtime)
    if not raw_videos:
        raise RuntimeError("Playwright did not produce a video")
    raw_video = raw_videos[-1]
    final_video = out_dir / "agenticsre_otel_demo.webm"
    raw_video.replace(final_video)
    mp4_video = out_dir / f"agenticsre_otel_demo_{VIDEO_HEIGHT}p.mp4"
    transcode = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(final_video),
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            VIDEO_CRF,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(mp4_video),
        ],
        text=True,
        capture_output=True,
    )
    if transcode.returncode != 0:
        (out_dir / "ffmpeg_transcode_error.log").write_text(
            (transcode.stdout or "") + "\n" + (transcode.stderr or ""),
            encoding="utf-8",
        )
        mp4_video = None
    frame_checks = extract_frame_checks(mp4_video or final_video, out_dir, frame_points)
    summary = {
        "output_dir": str(out_dir),
        "video": str(final_video),
        "mp4_video": str(mp4_video) if mp4_video else "",
        "video_size": {"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT, "crf": VIDEO_CRF},
        "narration": str(narration_path),
        "events": events,
        "frame_checks": frame_checks,
    }
    (out_dir / "recording_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def main() -> int:
    summary = record()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
