"""Multi-agent RCA must not inherit Skill capabilities from legacy config."""
import asyncio

import pytest

from configs.config_loader import AppConfig
from tools import build_tool_registry


@pytest.mark.parametrize("legacy_bridge", [False, True])
def test_multi_agent_tools_exclude_skills_even_with_legacy_switch(legacy_bridge):
    cfg = AppConfig()
    cfg.pipeline.enable_agent_skill_bridge = legacy_bridge
    registry = build_tool_registry(cfg, isolated=True)
    assert registry.get("skill_runtime_workflow") is None
    assert not registry.execute("skill_runtime_workflow", query="HTTP 503").success
    assert registry.get("kubectl") is not None


def test_explicit_skill_registry_does_not_contaminate_multi_agent():
    cfg = AppConfig()
    pure = build_tool_registry(cfg, isolated=True)
    skills = build_tool_registry(cfg, isolated=True, include_skill_runtime=True)
    assert skills.get("skill_runtime_workflow") is not None
    assert pure.get("skill_runtime_workflow") is None


def test_multi_agent_engine_rejects_skill_workflow_before_execution():
    from orchestrator.rca_engine import run_rca

    with pytest.raises(ValueError, match="Agent\\+Skill entry point"):
        asyncio.run(run_rca("HTTP 503", config=AppConfig(),
                            skill_workflow={"stages": ["diagnose"]}))


def test_multi_agent_engine_rejects_injected_skill_registry():
    from orchestrator.rca_engine import run_rca

    cfg = AppConfig()
    registry = build_tool_registry(cfg, isolated=True, include_skill_runtime=True)
    with pytest.raises(ValueError, match="Skill-capable"):
        asyncio.run(run_rca("HTTP 503", config=cfg, registry=registry))


def test_planning_prompt_does_not_offer_skill_tools():
    from agents.planning_agent import PlanningAgent
    class Model:
        def json_chat(self, messages, **kwargs):
            self.messages = messages
            return {"plan": []}
    model = Model()
    PlanningAgent(model, build_tool_registry(AppConfig(), isolated=True)).generate_plan([], "HTTP 503")
    assert "skill_runtime_workflow" not in str(model.messages)
    assert "Skill Runtime" not in str(model.messages)
