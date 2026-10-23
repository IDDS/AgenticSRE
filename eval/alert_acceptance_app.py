"""Launch the actual Web app with an isolated, non-secret acceptance config."""
import os

import configs.config_loader as loader


def main():
    if os.environ.get('AGENTICSRE_ACCEPTANCE_APP') != '1':
        raise SystemExit('isolated acceptance opt-in required')
    cfg = loader.AppConfig()
    cfg.llm.api_key = ''  # This experiment measures deterministic compression, not RCA.
    cfg.kubernetes.kubeconfig = '/run/acceptance-kube/config'
    cfg.kubernetes.use_ssh = False
    cfg.kubernetes.use_dry_run = False
    cfg.observability.prometheus_url = 'http://127.0.0.1:18100'
    cfg.observability.jaeger_url = os.environ['AGENTICSRE_ACCEPTANCE_JAEGER']
    cfg.alert.time_window = 30
    cfg.runtime.enable_self_healing = False
    cfg.remediation.enabled = False
    cfg.memory.enabled = False
    cfg.evolution.enabled = False
    cfg.fault_targets.k8s_clusters = [dict(id='default',name='Acceptance',kind='general',
        kubeconfig=cfg.kubernetes.kubeconfig,prometheus_url=cfg.observability.prometheus_url,
        jaeger_url=cfg.observability.jaeger_url)]
    loader._config = cfg
    import web_app.app as web
    web._state['config'] = cfg
    import uvicorn
    uvicorn.run(web.app,host='127.0.0.1',port=18086,log_level='warning')


if __name__ == '__main__':
    main()
