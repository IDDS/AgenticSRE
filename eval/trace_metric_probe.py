"""Disposable per-request metric probe, with exact OTLP Span references.

Bounded experiment only: per-request metric labels are not a production-scale
instrumentation recommendation. Only the last observation of each field is exposed.
"""
import json
import os
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

observations = {}
window_observations = deque()
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == '/metrics':
            time.sleep(int(os.environ.get('PROBE_METRICS_DELAY_MS', '0')) / 1000)
            with lock:
                lines = []
                if os.environ.get('PROBE_METRIC_MODE') == 'stable_window':
                    now = time.time()
                    while window_observations and window_observations[0][0] < now-10:
                        window_observations.popleft()
                    # Concurrent OTLP exports can finish out of request-end order.
                    rows = [r for r in window_observations if now-10 < r[0] <= now]
                    labels = dict(namespace=os.environ['POD_NAMESPACE'], pod=os.environ['POD_NAME'])
                    encoded = ','.join(k+'='+json.dumps(v) for k,v in labels.items())
                    values = {'window_duration_ms': sum(r[1] for r in rows)/len(rows) if rows else 0,
                              'window_error_ratio': sum(r[2]>=500 for r in rows)/len(rows) if rows else 0,
                              'window_requests': len(rows), 'window_end_seconds': now}
                    for field, value in values.items():
                        stamp = ''
                        lag_ms = int(os.environ.get('PROBE_SAMPLE_LAG_MS', '0'))
                        if lag_ms:
                            # Opt-in adversarial exporter: one field is timestamped
                            # differently. Production metrics retain the default.
                            lag = lag_ms if field == 'window_duration_ms' else 0
                            stamp = ' ' + str(int(now*1000)-lag)
                        lines.extend([f'# TYPE agenticsre_probe_{field} gauge',
                                      f'agenticsre_probe_{field}{{{encoded}}} {value}{stamp}'])
                for field, observation in observations.items():
                    if os.environ.get('PROBE_METRIC_MODE') == 'stable_window':
                        continue
                    labels = dict(namespace=os.environ['POD_NAMESPACE'], pod=os.environ['POD_NAME'],
                                  trace_id=observation['trace_id'], span_id=observation['span_id'])
                    encoded = ','.join(k+'='+json.dumps(v) for k,v in labels.items())
                    lines.extend([f'# TYPE agenticsre_probe_{field} gauge',
                                  f'agenticsre_probe_{field}{{{encoded}}} {observation["value"]}'])
            self.send_response(200);self.send_header('Content-Type','text/plain; version=0.0.4');self.end_headers()
            self.wfile.write(('\n'.join(lines)+'\n').encode());return
        if url.path == '/health':
            self.send_response(200);self.end_headers();self.wfile.write(b'ok');return
        if url.path != '/probe':
            self.send_error(404);return
        args=parse_qs(url.query)
        delay=min(1000,max(0,int(args.get('delay_ms',['0'])[0])))
        code=503 if args.get('fail',['0'])[0]=='1' else 200
        trace_id,span_id=uuid.uuid4().hex,uuid.uuid4().hex[:16]
        started=time.time_ns();time.sleep(delay/1000)
        # The Span end and measured window membership share one atomic boundary.
        # Export latency must not omit completed work from a concurrent scrape.
        with lock:
            ended=time.time_ns()
            duration=(ended-started)/1e6
            window_observations.append((ended/1e9, duration, code))
            while window_observations and window_observations[0][0] < ended/1e9-10:
                window_observations.popleft()
        attrs={'service.name':os.environ.get('PROBE_SERVICE_NAME') or os.environ['POD_NAME'],'k8s.cluster.name':'default',
               'k8s.namespace.name':os.environ['POD_NAMESPACE'],'k8s.pod.name':os.environ['POD_NAME'],'k8s.pod.uid':os.environ['POD_UID']}
        span=dict(traceId=trace_id,spanId=span_id,name='measured GET /probe',kind=2,
            startTimeUnixNano=str(started),endTimeUnixNano=str(ended),status={'code':2 if code>=500 else 1},
            attributes=[{'key':'http.status_code','value':{'intValue':str(code)}},{'key':'error','value':{'boolValue':code>=500}}])
        payload={'resourceSpans':[{'resource':{'attributes':[{'key':k,'value':{'stringValue':v}} for k,v in attrs.items()]},
                  'scopeSpans':[{'scope':{'name':'trace-metric-probe'},'spans':[span]}]}]}
        request=Request('http://jaeger.train-ticket.svc:4318/v1/traces',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        emit_trace = args.get('emit_trace', ['1'])[0] != '0'
        if emit_trace:
            with urlopen(request,timeout=5) as response:
                assert not json.load(response).get('partialSuccess',{}).get('rejectedSpans',0)
        update=args.get('update',['both'])[0]
        with lock:
            for field,value in [('duration_ms',duration),('http_status_code',code)]:
                if update in ('both',field):
                    observations[field]=dict(trace_id=trace_id,span_id=span_id,value=value)
        self.send_response(code);self.end_headers()
        self.wfile.write(json.dumps(dict(trace_id=trace_id,span_id=span_id,duration_ms=duration,status=code,
                                        update=update,trace_exported=emit_trace)).encode())

    def log_message(self,*args):pass


if __name__=='__main__':ThreadingHTTPServer(('0.0.0.0',8000),Handler).serve_forever()
