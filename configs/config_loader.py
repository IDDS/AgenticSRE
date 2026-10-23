"""
AgenticSRE Configuration Loader
Loads YAML config into strongly-typed dataclasses with env var substitution.
"""

import os
import re
import yaml
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from pathlib import Path

# ───────────── Dataclasses ─────────────

@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-v4-pro"
    temperature: float = 0.1
    max_tokens: int = 65536
    timeout: int = 300


@dataclass
class RuntimeConfig:
    mode: str = "daemon"
    log_level: str = "INFO"
    log_dir: str = "./logs"
    max_workers: int = 3
    enable_self_healing: bool = False


@dataclass
class KubernetesConfig:
    cluster_id: str = ""
    namespace: str = "default"
    kubeconfig: str = ""
    use_ssh: bool = False
    use_dry_run: bool = True
    forbid_unsafe_commands: List[str] = field(default_factory=list)
    ssh_jump_host: str = ""
    ssh_target: str = ""
    target_host: str = ""  # alias for ssh_target


@dataclass
class ObservabilityConfig:
    prometheus_url: str = ""
    otel_prometheus_url: str = ""
    elasticsearch_url: str = ""
    jaeger_url: str = ""
    grafana_url: str = ""


@dataclass
class DatabaseConfig:
    dsn: str = ""


@dataclass
class MCPConfig:
    external_servers: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class RemediationConfig:
    enabled: bool = False
    dry_run: bool = True
    max_steps: int = 5
    confidence_threshold: float = 0.85
    auto_confidence_threshold: float = 0.90
    auto_history_success_rate_threshold: float = 0.80
    auto_history_confidence_level: float = 0.95
    auto_history_min_samples: int = 14
    max_rollback_depth: int = 5
    require_approval: bool = True
    schedule: str = ""
    blackout_windows: List[str] = field(default_factory=list)
    max_auto_risk_level: str = "medium"
    canary_enabled: bool = False
    canary_namespace: str = "test"
    require_confirmation_for_high_risk: bool = True
    recommend_only: bool = True
    business_recovery_probes: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class MemoryConfig:
    enabled: bool = True
    backend: str = "chromadb"
    db_path: str = "./data/memory"
    embedding_model: str = "text-embedding-3-small"
    rules_collection: str = "sre_rules"
    faults_collection: str = "sre_faults"
    traces_collection: str = "sre_traces"
    max_similar_results: int = 5
    retrieval_timeout_s: float = 5.0
    auto_learn: bool = True
    judge_threshold: float = 0.65
    judge_llm_weight: float = 0.4


@dataclass
class ConfidenceCalibrationConfig:
    """Operator-tunable RCA confidence weights, evidence thresholds, and caps."""

    raw_weight: float = 0.18
    hypothesis_weight: float = 0.18
    evidence_weight: float = 0.27
    quality_weight: float = 0.17
    specificity_weight: float = 0.10
    history_weight: float = 0.10
    hypothesis_top_weight: float = 0.75
    evidence_coverage_weight: float = 0.45
    evidence_signal_weight: float = 0.35
    evidence_alignment_weight: float = 0.20
    quality_judge_weight: float = 0.65
    evidence_source_target: int = 4
    evidence_signal_target: float = 3.0
    evidence_alignment_target: int = 5
    min_evidence_sources_without_cap: int = 2
    min_sources_without_structured_evidence: int = 3
    min_root_cause_chars: int = 30
    specificity_cap_threshold: float = 0.55
    min_history_samples: int = 5
    history_confidence_level: float = 0.95
    low_history_diagnosis_accuracy: Optional[float] = None
    low_history_success_rate: float = 0.30  # deprecated compatibility alias
    default_cap: float = 0.95
    single_evidence_cap: float = 0.72
    missing_structured_evidence_cap: float = 0.78
    contradictory_evidence_cap: float = 0.70
    review_required_cap: float = 0.75
    low_specificity_cap: float = 0.65
    low_history_diagnosis_accuracy_cap: Optional[float] = None
    low_history_success_cap: float = 0.65  # deprecated compatibility alias


@dataclass
class DomainConfig:
    active_profile: str = "kubernetes"
    profiles_dir: str = ""           # empty = configs/domains/
    auto_detect: bool = False        # auto-detect domain from environment


@dataclass
class EvolutionConfig:
    enabled: bool = True
    snapshot_dir: str = ""           # empty = ./data/evolution/
    max_snapshots: int = 1000
    auto_record: bool = True         # record snapshot after each RCA


@dataclass
class DaemonConfig:
    poll_interval_seconds: int = 30
    dedup_ttl_seconds: int = 300
    max_concurrent_pipelines: int = 3
    default_namespace: str = ""
    status_file: str = "./data/daemon_status.json"
    signal_history_size: int = 100


@dataclass
class PipelineConfig:
    max_evidence_iterations: int = 3
    hypothesis_confidence_threshold: float = 0.85
    enable_correlation: bool = True
    # Optional final pass that preserves literal evidence identifiers and
    # rejects unsupported causal detail.  Disabled for legacy runs.
    enable_precision_verifier: bool = False
    # Generic literal-evidence coverage gate; disabled for legacy runs.
    enable_evidence_coverage_gate: bool = False
    enable_graph_rca: bool = True
    enable_recovery: bool = False
    enable_collaboration_optimization: bool = True
    enable_fast_evidence_selection: bool = True
    evidence_agent_timeout_s: int = 45
    rca_llm_budget_s: int = 900
    rca_llm_call_timeout_s: int = 120
    collaboration_reflection_timeout_s: int = 90
    evidence_summary_max_tokens: int = 1024
    enable_llm_inference_agent: bool = True
    enable_structured_evidence: bool = True
    # Legacy setting retained for loading old configs; multi-agent RCA ignores it.
    enable_agent_skill_bridge: bool = False
    skill_runtime_url: str = ""
    skill_runtime_skills_dir: str = ""
    agent_skill_bridge_mode: str = "tool_registry"
    skip_alert_compression_for_manual_rca: bool = True
    skip_planning_in_fast_evidence: bool = True


@dataclass
class AlertConfig:
    semantic_timeout_s: int = 60
    compression_enabled: bool = True
    time_window: int = 300
    similarity_threshold: float = 0.8
    max_group_size: int = 50


@dataclass
class MetricCheckConfig:
    name: str = ""
    query: str = ""
    unit: str = "%"
    label_key: str = "instance"
    ns_key: str = ""
    level: str = "node"
    warn: float = 85
    crit: float = 95


@dataclass
class DetectionConfig:
    sources_enabled: Dict[str, bool] = field(default_factory=lambda: {
        "prometheus": True,
        "k8s_event": True,
        "pod_health": True,
        "node_health": True,
        "metric_anomaly": True,
    })
    metric_checks: List[Dict[str, Any]] = field(default_factory=list)
    critical_event_reasons: List[str] = field(
        default_factory=lambda: ["OOMKilling", "NodeNotReady", "EvictionThresholdMet"]
    )
    critical_pod_reasons: List[str] = field(
        default_factory=lambda: ["CrashLoopBackOff", "OOMKilled"]
    )
    default_detect_methods: List[str] = field(
        default_factory=lambda: ["threshold", "zscore"]
    )
    default_algorithm: str = "zscore"
    default_lookback_m: int = 30
    default_z_threshold: float = 3.0
    default_ewma_span: int = 10
    min_samples: int = 12
    confirmation_points: int = 1
    relative_change_threshold: float = 0.0
    spectral_residual_threshold: float = 3.0
    categories_enabled: Dict[str, bool] = field(default_factory=lambda: {
        "infrastructure": True,
        "application": True,
        "business": True,
        "database": True,
        "k8s_workload": True,
    })
    business_services: List[str] = field(default_factory=lambda: [
        "nginx-thrift", "nginx-web", "compose-post", "user-service",
        "social-graph", "home-timeline", "user-timeline", "post-storage",
        "media-service", "url-shorten", "text-service",
    ])
    db_services: List[str] = field(default_factory=lambda: [
        "redis", "memcached", "mongodb", "mongo", "mysql", "postgres", "etcd",
    ])
    thresholds: Dict[str, float] = field(default_factory=dict)


@dataclass
class ObservabilityAgentConfig:
    trace_all_calls: bool = True
    collect_token_usage: bool = True
    collect_latency: bool = True
    behavior_validation: bool = True
    anomaly_threshold: float = 3.0


@dataclass
class ParadigmConfig:
    default: str = "plan_and_execute"
    max_react_steps: int = 8
    max_reflection_rounds: int = 2
    debate_perspectives: List[str] = field(
        default_factory=lambda: ["infrastructure", "application", "holistic"]
    )
    voting_temperatures: List[float] = field(
        default_factory=lambda: [0.1, 0.5, 0.8]
    )


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    enable_cors: bool = True


@dataclass
class K8sClusterProfile:
    id: str = ""
    name: str = ""
    kubeconfig: str = ""
    context: str = ""
    default_namespace: str = "default"
    description: str = ""


@dataclass
class LLMHostProfile:
    id: str = ""
    name: str = ""
    host: str = ""
    ssh_user: str = "root"
    ssh_key_path: str = ""
    jump_host: str = ""
    ssh_password_env: str = ""
    jump_password_env: str = ""
    gpu: str = ""
    role: str = ""


@dataclass
class FaultTargetsConfig:
    k8s_clusters: List[Dict[str, Any]] = field(default_factory=list)
    llm_hosts: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    kubernetes: KubernetesConfig = field(default_factory=KubernetesConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    remediation: RemediationConfig = field(default_factory=RemediationConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    confidence_calibration: ConfidenceCalibrationConfig = field(default_factory=ConfidenceCalibrationConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    observability_agent: ObservabilityAgentConfig = field(default_factory=ObservabilityAgentConfig)
    web: WebConfig = field(default_factory=WebConfig)
    paradigm: ParadigmConfig = field(default_factory=ParadigmConfig)
    domain: DomainConfig = field(default_factory=DomainConfig)
    evolution: EvolutionConfig = field(default_factory=EvolutionConfig)
    fault_targets: FaultTargetsConfig = field(default_factory=FaultTargetsConfig)


# ───────────── Loader ─────────────

_ENV_PATTERN = re.compile(r'\$\{(\w+)(?::([^}]*))?\}')

def _substitute_env(value: str) -> str:
    """Replace ${VAR} or ${VAR:default} with environment variables."""
    def _replacer(match):
        var_name = match.group(1)
        default = match.group(2) if match.group(2) is not None else ""
        return os.environ.get(var_name, default)
    return _ENV_PATTERN.sub(_replacer, value)


def _walk_and_substitute(obj):
    """Recursively substitute env vars in all string values."""
    if isinstance(obj, str):
        return _substitute_env(obj)
    elif isinstance(obj, dict):
        return {k: _walk_and_substitute(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_walk_and_substitute(v) for v in obj]
    return obj


def _dict_to_dataclass(dc_class, data: dict):
    """Convert a dict to a dataclass, ignoring unknown fields."""
    if not isinstance(data, dict):
        return dc_class()
    fieldnames = {f.name for f in dc_class.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in fieldnames}
    return dc_class(**filtered)


def _load_dotenv():
    """Load .env file from project root if present."""
    for candidate in [
        Path(__file__).parent.parent / ".env",
        Path.cwd() / ".env",
    ]:
        if candidate.exists():
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, _, value = line.partition('=')
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key and key not in os.environ:
                            os.environ[key] = value
            break


def _load_detection_config(data: dict) -> DetectionConfig:
    """Load DetectionConfig, keeping metric_checks as raw dicts."""
    if not isinstance(data, dict):
        return DetectionConfig()
    cfg = DetectionConfig()
    if "sources_enabled" in data and isinstance(data["sources_enabled"], dict):
        cfg.sources_enabled.update(data["sources_enabled"])
    if "metric_checks" in data and isinstance(data["metric_checks"], list):
        cfg.metric_checks = data["metric_checks"]
    if "critical_event_reasons" in data and isinstance(data["critical_event_reasons"], list):
        cfg.critical_event_reasons = data["critical_event_reasons"]
    if "critical_pod_reasons" in data and isinstance(data["critical_pod_reasons"], list):
        cfg.critical_pod_reasons = data["critical_pod_reasons"]
    if "default_detect_methods" in data and isinstance(data["default_detect_methods"], list):
        cfg.default_detect_methods = data["default_detect_methods"]
    if "default_algorithm" in data:
        cfg.default_algorithm = str(data["default_algorithm"])
    if "default_lookback_m" in data:
        cfg.default_lookback_m = int(data["default_lookback_m"])
    if "default_z_threshold" in data:
        cfg.default_z_threshold = float(data["default_z_threshold"])
    if "default_ewma_span" in data:
        cfg.default_ewma_span = int(data["default_ewma_span"])
    if "min_samples" in data:
        cfg.min_samples = int(data["min_samples"])
    if "confirmation_points" in data:
        cfg.confirmation_points = int(data["confirmation_points"])
    if "relative_change_threshold" in data:
        cfg.relative_change_threshold = float(data["relative_change_threshold"])
    if "spectral_residual_threshold" in data:
        cfg.spectral_residual_threshold = float(data["spectral_residual_threshold"])
    if "categories_enabled" in data and isinstance(data["categories_enabled"], dict):
        cfg.categories_enabled.update(data["categories_enabled"])
    if "business_services" in data and isinstance(data["business_services"], list):
        cfg.business_services = data["business_services"]
    if "db_services" in data and isinstance(data["db_services"], list):
        cfg.db_services = data["db_services"]
    if "thresholds" in data and isinstance(data["thresholds"], dict):
        cfg.thresholds = data["thresholds"]
    return cfg


def _load_fault_targets(raw_section: Any) -> FaultTargetsConfig:
    """Load FaultTargetsConfig from a dict (section may include k8s_clusters/llm_hosts lists)."""
    cfg = FaultTargetsConfig()
    if isinstance(raw_section, dict):
        if isinstance(raw_section.get("k8s_clusters"), list):
            cfg.k8s_clusters = list(raw_section["k8s_clusters"])
        if isinstance(raw_section.get("llm_hosts"), list):
            cfg.llm_hosts = list(raw_section["llm_hosts"])
    return cfg


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load configuration from YAML file with env var substitution."""
    # Load .env file if present (supports LLM_API_KEY etc.)
    _load_dotenv()

    if config_path is None:
        # Try multiple default locations
        candidates = [
            Path(__file__).parent / "config.yaml",
            Path.cwd() / "configs" / "config.yaml",
            Path.cwd() / "config.yaml",
        ]
        for p in candidates:
            if p.exists():
                config_path = str(p)
                break

    if config_path and Path(config_path).exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            raw = yaml.safe_load(f) or {}
        raw = _walk_and_substitute(raw)
    else:
        raw = {}

    # Fault-target profiles default to configs/clusters.yaml when main config omits the section
    fault_targets_raw = raw.get('fault_targets')
    if fault_targets_raw is None:
        clusters_path = Path(__file__).parent / "clusters.yaml"
        if clusters_path.exists():
            try:
                with open(clusters_path, 'r', encoding='utf-8') as f:
                    fallback = yaml.safe_load(f) or {}
                fault_targets_raw = _walk_and_substitute(fallback).get('fault_targets')
            except Exception:
                fault_targets_raw = None

    return AppConfig(
        llm=_dict_to_dataclass(LLMConfig, raw.get('llm', {})),
        runtime=_dict_to_dataclass(RuntimeConfig, raw.get('runtime', {})),
        kubernetes=_dict_to_dataclass(KubernetesConfig, raw.get('kubernetes', {})),
        observability=_dict_to_dataclass(ObservabilityConfig, raw.get('observability', {})),
        database=_dict_to_dataclass(DatabaseConfig, raw.get('database', {})),
        mcp=_dict_to_dataclass(MCPConfig, raw.get('mcp', {})),
        remediation=_dict_to_dataclass(RemediationConfig, raw.get('remediation', {})),
        memory=_dict_to_dataclass(MemoryConfig, raw.get('memory', {})),
        confidence_calibration=_dict_to_dataclass(
            ConfidenceCalibrationConfig, raw.get('confidence_calibration', {})
        ),
        daemon=_dict_to_dataclass(DaemonConfig, raw.get('daemon', {})),
        pipeline=_dict_to_dataclass(PipelineConfig, raw.get('pipeline', {})),
        alert=_dict_to_dataclass(AlertConfig, raw.get('alert', {})),
        detection=_load_detection_config(raw.get('detection', {})),
        observability_agent=_dict_to_dataclass(ObservabilityAgentConfig, raw.get('observability_agent', {})),
        web=_dict_to_dataclass(WebConfig, raw.get('web', {})),
        paradigm=_dict_to_dataclass(ParadigmConfig, raw.get('paradigm', {})),
        domain=_dict_to_dataclass(DomainConfig, raw.get('domain', {})),
        evolution=_dict_to_dataclass(EvolutionConfig, raw.get('evolution', {})),
        fault_targets=_load_fault_targets(fault_targets_raw),
    )


# ───────────── Singleton ─────────────

_config: Optional[AppConfig] = None

def get_config(config_path: Optional[str] = None) -> AppConfig:
    """Get the singleton AppConfig instance."""
    global _config
    if _config is None:
        _config = load_config(config_path)
    return _config


def reload_config(config_path: Optional[str] = None) -> AppConfig:
    """Force reload configuration."""
    global _config
    _config = load_config(config_path)
    return _config
