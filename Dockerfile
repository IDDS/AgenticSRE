# AgenticSRE container image.

ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        openssh-client \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY docker-deps/kubectl /usr/local/bin/kubectl
RUN chmod +x /usr/local/bin/kubectl

WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 5 \
    --index-url "${PIP_INDEX_URL}" \
    --trusted-host "${PIP_TRUSTED_HOST}" \
    -r requirements.txt

COPY hermes-agent/ ./hermes-agent/
RUN pip install --no-cache-dir --timeout 120 --retries 5 \
    --index-url "${PIP_INDEX_URL}" \
    --trusted-host "${PIP_TRUSTED_HOST}" \
    ./hermes-agent
RUN cd /app/hermes-agent && python -c "from run_agent import AIAgent"

COPY agents/ ./agents/
COPY configs/ ./configs/
COPY eval/ ./eval/
COPY integrations/ ./integrations/
COPY memory/ ./memory/
COPY observability/ ./observability/
COPY orchestrator/ ./orchestrator/
COPY paradigms/ ./paradigms/
COPY skill_runtime/ ./skill_runtime/
COPY tools/ ./tools/
COPY vllm_fault_injector/ ./vllm_fault_injector/
COPY web_app/ ./web_app/
COPY main.py mcp_server.py operational_ontology.py operational_lineage.py operational_provenance.py diagnostic_strategy.py recovery_verification.py README.md USER_MANUAL.md AGENTS.md ./

VOLUME ["/app/data", "/app/logs"]

ENV PYTHONPATH=/app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1

CMD ["python", "-m", "uvicorn", "web_app.app:app", "--host", "0.0.0.0", "--port", "8080"]
