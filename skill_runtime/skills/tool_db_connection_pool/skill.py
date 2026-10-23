from __future__ import annotations

from typing import Any, Dict


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _db_kind(payload: Dict[str, Any]) -> str:
    raw = str(payload.get("db_kind") or payload.get("database") or "").lower()
    if "mysql" in raw:
        return "mysql"
    if "postgres" in raw or "pgsql" in raw or "psql" in raw:
        return "postgres"
    return "generic"


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = " ".join(str(payload.get(k, "")) for k in ("message", "fault_type", "root_cause", "database", "service")).lower()
    matched = any(token in text for token in ("db", "database", "postgres", "mysql", "connection pool", "too many connections", "lock wait", "deadlock", "timeout"))
    evidence = []
    if "too many connections" in text or "connection pool" in text:
        evidence.append("连接池或最大连接数异常")
    if "lock wait" in text or "deadlock" in text:
        evidence.append("存在锁等待或死锁信号")
    if "timeout" in text:
        evidence.append("数据库请求超时")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.8 if matched else 0.0,
        "summary": "命中 DB 连接池/锁等待诊断工具 skill" if matched else "未命中 DB 工具 skill",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    kind = _db_kind(payload)
    if kind == "postgres":
        commands = [
            "psql \"$DATABASE_URL\" -c \"select count(*) as connections from pg_stat_activity;\"",
            "psql \"$DATABASE_URL\" -c \"select state, count(*) from pg_stat_activity group by state;\"",
            "psql \"$DATABASE_URL\" -c \"select pid, wait_event_type, wait_event, query from pg_stat_activity where wait_event is not null limit 20;\"",
        ]
    elif kind == "mysql":
        commands = [
            "mysql \"$DATABASE_URL\" -e \"show status like 'Threads_connected';\"",
            "mysql \"$DATABASE_URL\" -e \"show processlist;\"",
            "mysql \"$DATABASE_URL\" -e \"show engine innodb status\\G\"",
        ]
    else:
        commands = [
            "检查 DB 连接数、连接池 active/idle/waiting 指标",
            "检查慢查询、锁等待、deadlock 和 timeout 日志",
        ]
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "生成 DB 连接池/锁等待只读诊断计划。",
        "evidence": list((context.get("detect") or {}).get("evidence") or []) + ["DB 工具 skill 默认只读"],
        "db_kind": kind,
        "commands": commands,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "confidence": 0.7,
        "summary": "验证 DB 连接数、等待队列、错误率和业务请求时延是否恢复。",
        "evidence": ["只读验证计划"],
        "checks": [
            "检查连接池 active/idle/waiting 指标",
            "检查 DB timeout/error rate 是否下降",
            "检查业务接口 P95/P99 是否恢复",
        ],
    }
