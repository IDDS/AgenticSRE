"""Actual HTTP/Prometheus/Jaeger acceptance using frozen application image.

No production configuration is copied. Existing Kubernetes credentials are
mounted read-only; model credentials are neither loaded nor used.
"""
from functools import partial
import json
import os
from pathlib import Path
import socket
import subprocess

from sustained_alert_acceptance import sustained
from trace_alert_acceptance import main as experiment


def start_app(*,out,image,name,owner,jaeger):
    for folder in ('data','logs'):
        (out/folder).mkdir()
    argv = ['docker','run','-d','--pull','never','--name',name,'--label','agenticsre.experiment='+owner,
        '--network','host','--cpus','2','--memory','4g','--no-healthcheck',
        '-e','PYTHONPATH=/app','-e','AGENTICSRE_ACCEPTANCE_APP=1',
        '-e','AGENTICSRE_AUTH_ENABLED=0','-e','AGENTICSRE_ALERT_EMBEDDING_MODEL=all-MiniLM-L6-v2',
        '-e','AGENTICSRE_ACCEPTANCE_JAEGER='+jaeger,
        '-v',str(Path(__file__).resolve().parent)+':/acceptance:ro',
        '-v','/home/ubuntu/.kube:/run/acceptance-kube:ro',
        '-v',str(out/'data')+':/app/data','-v',str(out/'logs')+':/app/logs',
        '--entrypoint','python',image,'/acceptance/alert_acceptance_app.py']
    subprocess.run(argv,check=True,stdout=subprocess.DEVNULL,timeout=90)


def main():
    if os.environ.get('AGENTICSRE_RUN_ALERT_ACCEPTANCE') != '1':
        raise SystemExit('explicit isolated acceptance opt-in required')
    image = os.environ['AGENTICSRE_EXPERIMENT_IMAGE']
    if not image.startswith('sha256:') or len(image) != 71:
        raise ValueError('immutable image ID required')
    root = Path(os.environ['AGENTICSRE_EXPERIMENT_ROOT'])
    contract = json.loads((root/'contract.json').read_text())
    if image != contract['candidate_image']:
        raise ValueError('image differs from frozen contract')
    # Fail before creating resources if historical runner ports are occupied.
    sockets=[]
    try:
        for port in (18086,18100,18101,18102,18103,17686):
            sock=socket.socket();sockets.append(sock)
            sock.bind(('127.0.0.1',port))
    finally:
        for sock in sockets: sock.close()
    os.environ['AGENTICSRE_RUN_TRACE_ALERT_EXPERIMENT']='1'
    experiment(scenario=partial(sustained,stable_window=True,window_merge=True,
        extended=True,all_scenarios=True),metric_mode='stable_window',window_proofs=True,start_app=start_app)


if __name__ == '__main__':
    main()
