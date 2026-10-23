"""The measurement window must include completed work while Trace export waits."""
import importlib.util
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen


def test_completed_request_is_measured_before_slow_trace_export(monkeypatch):
    spec=importlib.util.spec_from_file_location('window_probe_under_test',Path(__file__).parents[1]/'eval/trace_metric_probe.py')
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    for key,value in dict(POD_NAMESPACE='ns',POD_NAME='pod',POD_UID='uid',PROBE_METRIC_MODE='stable_window').items():
        monkeypatch.setenv(key,value)
    exporting,release=threading.Event(),threading.Event()
    def export(*args,**kwargs):
        exporting.set()
        assert release.wait(10)
        return io.StringIO('{}')
    monkeypatch.setattr(probe,'urlopen',export)
    server=probe.ThreadingHTTPServer(('127.0.0.1',0),probe.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    def request():
        with urlopen(base+'/probe?delay_ms=0',timeout=15) as response:return response.read()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(request)
            try:
                assert exporting.wait(5)
                with urlopen(base+'/metrics',timeout=5) as response:metrics=response.read().decode()
                assert 'agenticsre_probe_window_requests{namespace="ns",pod="pod"} 1' in metrics
            finally:
                release.set()
                pending.result(timeout=15)
    finally:
        release.set();server.shutdown();server.server_close();thread.join(timeout=5)
