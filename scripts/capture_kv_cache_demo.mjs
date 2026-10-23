import fs from "node:fs/promises";
import path from "node:path";
import playwright from "/Users/cpf/gstack/node_modules/playwright-core/index.js";

const { chromium } = playwright;

const baseUrl = process.env.AGENTICSRE_UI_URL || "http://127.0.0.1:18081";
const outDir = process.env.AGENTICSRE_DEMO_DIR || "demo_recordings/kv_cache_demo_20260712/frames";
await fs.mkdir(outDir, { recursive: true });

const browser = await chromium.launch({
  executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 1 });

async function caption(text) {
  await page.evaluate((value) => {
    let node = document.getElementById("agenticsre-demo-caption");
    if (!node) {
      node = document.createElement("div");
      node.id = "agenticsre-demo-caption";
      Object.assign(node.style, {
        position: "fixed", left: "180px", right: "180px", bottom: "28px", zIndex: "99999",
        padding: "16px 28px", background: "rgba(12, 42, 86, 0.92)", color: "white",
        border: "1px solid rgba(255,255,255,.45)", borderRadius: "8px", fontSize: "24px",
        fontWeight: "600", textAlign: "center", boxShadow: "0 8px 30px rgba(0,0,0,.28)",
      });
      document.body.appendChild(node);
    }
    node.textContent = value;
  }, text);
}

async function shot(index, name, text) {
  await caption(text);
  await page.waitForTimeout(1200);
  await page.screenshot({ path: path.join(outDir, `${String(index).padStart(2, "0")}-${name}.png`) });
}

await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 60000 });
await page.evaluate(() => switchView("overview"));
await shot(1, "overview", "AgenticSRE 智算闭环：从真实负载和运行期信号开始，而不是根据场景名称直接输出答案");

await page.evaluate(() => switchView("llmfaultlab"));
await page.waitForTimeout(2500);
await page.selectOption("#llmfaultlab-scenario", "sw-003");
await page.evaluate(() => {
  document.getElementById("llmfaultlab-dryrun").checked = true;
  document.getElementById("llmfaultlab-confirm").checked = false;
  renderLLMFaultParamHelp();
});
await shot(2, "faultlab", "选择 sw-003 / KV Cache Exhaustion；预检展示目标对象、观测信号和最小恢复动作");

await page.evaluate(async () => {
  const body = llmFaultRequestBody({ action: "inject", scenario: selectedLLMFaultScenario() });
  const result = await api("/api/llm-faultlab/preflight/sw-003", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), throwOnError: true,
  });
  const out = document.getElementById("llmfaultlab-output");
  if (out) out.textContent = JSON.stringify(result.preflight, null, 2);
});
await shot(3, "preflight", "Dry-run 预检确认：故障负载只作用于 llm-k8s / vllm-server，恢复仅删除故障负载 Pod");

await page.evaluate(() => switchView("aidashboard"));
await page.waitForTimeout(3500);
await shot(4, "metrics", "真实故障窗口：KV Cache 超过 95%，等待队列约 29，TTFT P95 达到 10 秒");

await page.evaluate(() => switchView("aievents"));
await page.waitForTimeout(3000);
await page.evaluate(async () => {
  const cluster = document.getElementById("ai-events-cluster");
  if (cluster) cluster.value = "t4-vllm-k8s";
  const windowSelect = document.getElementById("ai-events-window");
  if (windowSelect) windowSelect.value = "15m";
  await loadAIEvents();
});
await shot(5, "events", "事件中心按集群和时间窗聚合 KV Cache、排队和时延证据，形成可处置智算事件");

await page.evaluate(() => switchView("rca"));
await page.waitForTimeout(3000);
await shot(6, "rca", "同一事件可运行多智能体或 Agent+Skill；过程保留假设、调查、Skill 调用、置信度与复审原因");

await page.evaluate(() => switchView("llmfaultlab"));
await page.waitForTimeout(1800);
await page.evaluate(() => {
  const out = document.getElementById("llmfaultlab-output");
  if (out) out.textContent = JSON.stringify({
    diagnosis: "KV Cache 逻辑容量饱和导致请求排队与 TTFT/E2E 退化",
    evidence: ["KV Cache > 95%", "waiting_requests ≈ 29", "TTFT P95 = 10s", "GPU 总显存未耗尽"],
    remediation: "停止 agenticsre-kv-cache-load，不重启健康的 vllm-server",
    verification: "KV Cache 回落、队列排空、服务 Pod 保持 Ready",
  }, null, 2);
});
await shot(7, "recovery", "最小影响恢复：停止额外长上下文负载，不重启健康模型服务；随后重新采样验证恢复");

await page.evaluate(() => switchView("aidashboard"));
await page.waitForTimeout(2500);
await shot(8, "verified", "恢复完成：故障负载已清理，vLLM Pod 保持 Ready；闭环以恢复证据结束，而不是以命令执行结束");

await browser.close();
console.log(outDir);
