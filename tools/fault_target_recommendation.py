"""Tool adapter for read-only fault target proposals."""
from typing import Any

from skill_runtime.fault_target_selector import recommend_targets
from tools.base_tool import SRETool, ToolResult


class FaultTargetRecommendationTool(SRETool):
    name = 'fault_target_recommendation'
    description = 'Rank UID-bound Kubernetes fault-test candidates from an observed topology; does not authorize execution'

    def _execute(self, resources: list[dict[str, Any]], cluster_id: str, allowed_namespaces: list[str],
                 allowed_deployment_uids: list[str], observed_at: str,
                 max_services: int = 1, max_ready_backends: int = 10) -> ToolResult:
        try:
            result = recommend_targets(resources, cluster_id=cluster_id, allowed_namespaces=allowed_namespaces,
                                       allowed_deployment_uids=allowed_deployment_uids, observed_at=observed_at, max_services=max_services,
                                       max_ready_backends=max_ready_backends)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        return ToolResult(success=True, data=result)

    def _parameters_schema(self):
        return {'type': 'object', 'properties': {
            'resources': {'type': 'array', 'maxItems': 10000, 'items': {'type': 'object'}},
            'cluster_id': {'type': 'string'}, 'allowed_namespaces': {'type': 'array', 'items': {'type': 'string'}},
            'allowed_deployment_uids': {'type': 'array', 'minItems': 1, 'maxItems': 1000, 'items': {'type': 'string'}},
            'observed_at': {'type': 'string', 'description': 'Timezone-qualified acquisition time; not a freshness guarantee'},
            'max_services': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 1},
            'max_ready_backends': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 10},
        }, 'required': ['resources', 'cluster_id', 'allowed_namespaces', 'allowed_deployment_uids', 'observed_at']}
