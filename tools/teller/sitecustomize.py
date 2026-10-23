"""Auto-load AgenticSRE TELLER hooks when PYTHONPATH points to tools/teller."""

import os

if os.environ.get("AGENTICSRE_TELLER_HOOKS"):
    try:
        import vllm_http_pytorch_hooks

        vllm_http_pytorch_hooks.install()
    except Exception:
        pass
