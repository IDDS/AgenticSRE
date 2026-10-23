"""Baselines package for comparison evaluation."""
from eval.baselines.direct_llm_baseline import DirectLLMBaseline
from eval.baselines.hermes_agent_baseline import HermesAgentBaseline

__all__ = ["DirectLLMBaseline", "HermesAgentBaseline"]
