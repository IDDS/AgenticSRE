"""Read-only Service diagnosis using openJiuwen's WorkSwarm process SDK."""
import asyncio
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from integrations.hermes_skill_runtime import HermesSkillRuntimeClient

DNS_LABEL = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
TOOLS = {
    "read_service": "Read the selected Kubernetes Service and its selector.",
    "read_endpoints": "Read EndpointSlices for the selected Service, including Pod references and readiness.",
    "read_pods": "Read readiness and phase of Pods matching the selected Service selector.",
}
CLUSTER_TOOLS = frozenset(TOOLS)
TOOLS.update({
    "list_diagnostic_skills": "List AgenticSRE diagnostic skills available to this Agent.",
    "run_diagnostic_skill": "Run a discovered Skill in detect and diagnose stages, dry-run only.",
})
SKILL_INPUT = {
    "type": "object", "properties": {"skill_id": {"type": "string", "maxLength": 128}},
    "required": ["skill_id"], "additionalProperties": False,
}
EMPTY_INPUT = {"type": "object", "properties": {}, "additionalProperties": False}
TOOL_DEFINITIONS = [
    {"name": name, "description": description,
     "input_schema": SKILL_INPUT if name == "run_diagnostic_skill" else EMPTY_INPUT}
    for name, description in TOOLS.items()
]


class Turn(BaseModel):
    namespace: str = Field(pattern=DNS_LABEL)
    service: str = Field(pattern=DNS_LABEL)
    question: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


def process_client(llm):
    from jiuwenswarm_sdk import Client
    binary = shutil.which(os.getenv("WORKSWARM_BIN", "/opt/workswarm/bin/jiuwenswarm-process"))
    if not binary:
        raise RuntimeError("WorkSwarm 运行时未安装")
    workspace = Path(os.getenv("WORKSWARM_WORKSPACE", "/tmp/agenticsre-workswarm-workspace"))
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    client = Client([binary], cwd=str(workspace), env={
        "JIUWENSWARM_DATA_DIR": os.getenv("WORKSWARM_DATA_DIR", "/opt/workswarm/.jiuwenswarm"),
        "API_KEY": llm.api_key or "", "API_BASE": llm.base_url or "",
        "MODEL_NAME": llm.model or "", "MODEL_PROVIDER": "OpenAI",
    })
    # The SDK merges env with the host environment. Its model transport treats
    # a present but empty proxy as a URL, so omit it only in the child.
    proxy_keys = {"http_proxy", "https_proxy", "all_proxy", "ftp_proxy"}
    client.env = {key: value for key, value in client.env.items()
                  if key.lower() not in proxy_keys or value.strip()}
    return client


class EvidenceUnavailable(ValueError):
    """Safe diagnostic for display to users and the Agent."""


def endpoint_observation(slices):
    readiness = {"true": 0, "false": 0, "unspecified": 0}
    sample = []
    shown = 0
    for item in slices:
        endpoints = item.get("endpoints") or []
        rows = []
        include_slice = len(sample) < 20
        for endpoint in endpoints:
            ready = (endpoint.get("conditions") or {}).get("ready")
            state = "true" if ready is True else "false" if ready is False else "unspecified"
            readiness[state] += 1
            if include_slice and len(rows) < 30:
                ref = endpoint.get("targetRef") or {}
                rows.append({"target": ref.get("name"), "target_kind": ref.get("kind"), "ready": ready})
        if include_slice:
            shown += len(rows)
            sample.append({"name": item["metadata"]["name"], "count": len(endpoints), "endpoints": rows})
    total = sum(readiness.values())
    return {"count": len(slices), "slices": sample, "readiness": readiness,
            "endpoint_count": total, "shown_endpoint_count": shown,
            "truncated": shown < total or len(sample) < len(slices)}


async def read_evidence(name, namespace, service, read_json):
    async def read(command):
        result = await read_json(command, namespace)
        if not isinstance(result, dict):
            raise EvidenceUnavailable("集群返回了无法解析的数据")
        if "error" in result or result.get("status") == "Failure":
            detail = str(result.get("reason") or result.get("error") or "").lower()
            if "forbidden" in detail:
                raise EvidenceUnavailable("集群拒绝读取权限")
            if "notfound" in detail or "not found" in detail:
                raise EvidenceUnavailable("集群中未找到目标对象")
            if "timeout" in detail or "timed out" in detail:
                raise EvidenceUnavailable("集群读取超时")
            raise EvidenceUnavailable("集群读取失败")
        return result

    if name == "read_endpoints":
        result = await read(f"get endpointslices -l kubernetes.io/service-name={service}")
        return endpoint_observation(result["items"])
    resource = await read(f"get service {service}")
    spec = resource["spec"]
    selector = spec.get("selector") or {}
    if name == "read_service":
        return {"name": resource["metadata"]["name"], "type": spec.get("type"), "selector": selector}
    if name != "read_pods":
        raise ValueError("未知工具")
    if not selector:
        return {"selector": {}, "reason": "Service 未设置 Pod 选择器"}
    # These values originate in the API server but also cross the shell boundary.
    if not all(re.fullmatch(r"[A-Za-z0-9._/-]+", key) and re.fullmatch(r"[A-Za-z0-9._-]*", value)
               for key, value in selector.items()):
        raise ValueError("无效的 Service 选择器")
    labels = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
    pods = (await read(f"get pods -l {labels}"))["items"]
    return {"selector": selector, "count": len(pods), "truncated": len(pods) > 30, "pods": [
        {"name": pod["metadata"]["name"], "phase": pod.get("status", {}).get("phase"),
         "ready": next((c.get("status") for c in pod.get("status", {}).get("conditions", [])
                        if c.get("type") == "Ready"), None)} for pod in pods[:30]]}


def build_router(get_config, configured, read_json):
    router = APIRouter(prefix="/api/workswarm")
    sessions = {}
    tasks = set()

    @router.get("/capabilities")
    async def capabilities():
        llm = get_config().llm
        error = ""
        try:
            client = process_client(llm)
            reply = await client.query("protocol.capabilities", deadline_seconds=12)
            if not reply.get("data", {}).get("features", {}).get("host_tools"):
                error = "运行时缺少宿主工具协议"
        except ImportError:
            error = "WorkSwarm SDK 未安装"
        except Exception:
            error = "WorkSwarm 运行时不可用"
        if not error and not configured(llm):
            error = "模型未配置"
        return {"available": not error, "error": error, "model": llm.model,
                "runtime": "openJiuwen / WorkSwarm", "tools": list(TOOLS)}

    async def run(session, turn, client, question):
        skills = HermesSkillRuntimeClient(timeout_s=30, source="workswarm")

        async def tool(event):
            payload = event["payload"]
            name = payload["name"]
            arguments = payload.get("arguments") or {}
            if name not in TOOLS or (name != "run_diagnostic_skill" and arguments):
                raise ValueError("无效的工具调用")
            record = {"tool": name, "call_id": payload["call_id"], "status": "reading"}
            turn["evidence"].append(record)
            try:
                if name == "list_diagnostic_skills":
                    catalog = await asyncio.to_thread(skills.list_skills, stage="diagnose", limit=30)
                    observation = {"count": catalog["matched_count"], "skills": catalog["skills"]}
                elif name == "run_diagnostic_skill":
                    skill_id = arguments["skill_id"]
                    if not re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9_.-]{0,127}", skill_id):
                        raise ValueError("无效的 Skill ID")
                    record["skill_id"] = skill_id
                    observation = await asyncio.to_thread(
                        skills.run_skill, skill_id, question, namespace=session["namespace"],
                        stages=["detect", "diagnose"], context={"service": session["service"]},
                    )
                    if observation["status"] not in {"ok", "success", "completed"}:
                        record.update(status="error", observation=observation)
                        return observation
                else:
                    observation = await read_evidence(name, session["namespace"], session["service"], read_json)
            except Exception as exc:
                message = str(exc) if isinstance(exc, EvidenceUnavailable) else "工具读取失败，证据不足"
                record.update(status="error", observation={"error": message})
                return record["observation"]
            record.update(status="ok", observation=observation)
            return observation

        try:
            request = {
                "input": f"目标 {session['namespace']}/{session['service']}。用户问题：{question}",
                "mode": "agent.work.normal",
                "agent": {"name": "agenticsre-service-diagnosis", "tools": list(TOOLS),
                          "instructions": "以中文回答。每轮使用只读工具核对目标 Service、EndpointSlice 和 Pod。"
                                          "先给出结论，再用简短列表引用具体对象和就绪状态，说明证据不足或列表截断。"},
                "skills": [], "mcp": [], "host_tools": TOOL_DEFINITIONS,
                "timeout_seconds": 180, "max_turns": 10,
            }
            if session["runtime_session"]:
                request["session_id"] = session["runtime_session"]
            result = await client.run(request, on_tool_call=tool, deadline_seconds=210)
            latest = {item["tool"]: item["status"] for item in turn["evidence"]}
            observed = {name for name in CLUSTER_TOOLS if latest.get(name) == "ok"}
            turn["missing_tools"] = sorted(CLUSTER_TOOLS - observed)
            if result.get("status") != "completed":
                code = (result.get("error") or {}).get("code", "RUNTIME_ERROR")
                turn["error"] = f"WorkSwarm 运行失败（{code}）"
            elif not observed:
                turn["error"] = "本轮未取得集群证据"
            elif not result.get("output"):
                turn["error"] = "WorkSwarm 未返回诊断结论"
            else:
                session["runtime_session"] = result["session_id"]
                turn["answer"] = result["output"]
                turn["partial"] = bool(turn["missing_tools"]) or "error" in latest.values()
        except Exception as exc:
            turn["error"] = f"WorkSwarm 调用失败（{type(exc).__name__}）"
        finally:
            session["status"] = "error" if turn.get("error") else ("partial" if turn.get("partial") else "completed")

    @router.post("/turn", status_code=202)
    async def start(body: Turn):
        question = body.question.strip()
        if not question:
            raise HTTPException(422, "请输入诊断问题")
        llm = get_config().llm
        if not configured(llm):
            raise HTTPException(503, "模型未配置")
        try:
            client = process_client(llm)
        except (ImportError, OSError, RuntimeError):
            raise HTTPException(503, "WorkSwarm SDK 或运行时不可用")
        if len(tasks) >= 4:
            raise HTTPException(429, "运行中的诊断已达上限")
        sid = body.session_id
        if sid:
            session = sessions.get(sid)
            if not session:
                raise HTTPException(404, "会话已过期")
            if session["status"] == "running" or (session["namespace"], session["service"]) != (body.namespace, body.service):
                raise HTTPException(409, "会话忙或诊断对象已改变")
            if len(session["turns"]) >= 20:
                raise HTTPException(409, "此会话已满，请新建会话")
        else:
            if len(sessions) >= 100:
                oldest = next(key for key, item in sessions.items() if item["status"] != "running")
                del sessions[oldest]
            sid = uuid.uuid4().hex
            session = sessions[sid] = {"namespace": body.namespace, "service": body.service,
                                       "turns": [], "runtime_session": None}
        turn = {"question": question, "evidence": [], "started": time.time()}
        session["turns"].append(turn)
        session["status"] = "running"
        task = asyncio.create_task(run(session, turn, client, question))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"session_id": sid}

    @router.get("/sessions/{sid}")
    async def get_session(sid: str):
        if sid not in sessions:
            raise HTTPException(404, "会话已过期")
        return {key: value for key, value in sessions[sid].items() if key != "runtime_session"}

    return router
