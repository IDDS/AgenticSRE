from agents.hypothesis_agent import Hypothesis, HypothesisAgent


class FakeLLM:
    def __init__(self, response):
        self.response = response

    def json_chat(self, _messages, **kwargs):
        return self.response


def base_hypotheses():
    return [
        Hypothesis(id="H1", description="数据库写入延迟导致请求超时", confidence=0.6),
        Hypothesis(id="H2", description="下游依赖响应变慢", confidence=0.4),
    ]


def test_rerank_adds_evidence_supported_uncovered_branch():
    agent = HypothesisAgent(FakeLLM({
        "hypotheses": [
            {"id": "H1", "confidence": 0.1, "status": "rejected"},
            {"id": "H2", "confidence": 0.2, "status": "active"},
        ],
        "new_hypotheses": [{
            "description": "同节点新增计算负载造成 CPU 争用与容器节流",
            "confidence": 0.92,
            "status": "confirmed",
            "service": "compose-post-service",
            "fault_type": "cpu_contention",
            "supporting_evidence": ["CPU 99%", "移除新增负载后恢复"],
        }],
    }))

    result = agent.rerank(base_hypotheses(), {"metric": {"summary": "CPU 99%"}})

    assert len(result) == 3
    assert result[0].id == "H3"
    assert result[0].confidence == 0.92
    assert result[0].fault_type == "cpu_contention"


def test_rerank_branch_expansion_can_be_disabled():
    agent = HypothesisAgent(FakeLLM({
        "hypotheses": [],
        "new_hypotheses": [{
            "description": "证据支持的新根因分支",
            "confidence": 0.9,
        }],
    }))

    result = agent.rerank(base_hypotheses(), {}, allow_branch_expansion=False)

    assert len(result) == 2


def test_rerank_deduplicates_new_branch_and_respects_cap():
    agent = HypothesisAgent(FakeLLM({
        "hypotheses": [],
        "new_hypotheses": [
            {"description": "数据库写入延迟导致请求超时", "confidence": 0.9},
            {"description": "CPU 资源争用导致业务请求排队", "confidence": 0.8},
        ],
    }))

    result = agent.rerank(base_hypotheses(), {}, max_hypotheses=3)

    assert len(result) == 3
    assert result[0].description == "CPU 资源争用导致业务请求排队"


def test_rerank_remains_compatible_with_legacy_response():
    agent = HypothesisAgent(FakeLLM({
        "hypotheses": [{"id": "H1", "confidence": 0.75, "status": "active"}],
    }))

    result = agent.rerank(base_hypotheses(), {})

    assert len(result) == 2
    assert result[0].id == "H1"
    assert result[0].confidence == 0.75


def test_rejected_branch_cannot_win_with_inconsistent_confidence():
    agent = HypothesisAgent(FakeLLM({
        "hypotheses": [
            {"id": "H1", "confidence": 0.99, "status": "rejected"},
            {"id": "H2", "confidence": 0.55, "status": "active"},
        ],
    }))

    result = agent.rerank(base_hypotheses(), {})

    assert result[0].id == "H2"
    assert result[0].status == "active"
    assert result[1].id == "H1"
    assert result[1].confidence == 0.0


def test_rejected_new_branch_is_not_added():
    agent = HypothesisAgent(FakeLLM({
        "hypotheses": [],
        "new_hypotheses": [{
            "description": "证据已经反驳的新分支",
            "confidence": 0.99,
            "status": "rejected",
        }],
    }))

    result = agent.rerank(base_hypotheses(), {})

    assert len(result) == 2
