"""Disposable live HTTP dependency probe; OTLP spans describe actual requests."""
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

FAILED = False
STATE_CHANGE_ID = None
ROLE = os.environ['ROLE']
DOWNSTREAM = os.environ.get('DOWNSTREAM', '')


def transmit(span):
    attrs = {'service.name': os.environ['SERVICE_NAME'], 'k8s.cluster.name': 'default',
             'k8s.namespace.name': os.environ['POD_NAMESPACE'], 'k8s.pod.name': os.environ['POD_NAME'],
             'k8s.pod.uid': os.environ['POD_UID']}
    payload = {'resourceSpans': [{'resource': {'attributes': [{'key': k, 'value': {'stringValue': v}} for k, v in attrs.items()]},
                                'scopeSpans': [{'scope': {'name': 'agenticsre-live-propagation-probe'}, 'spans': [span]}]}]}
    req = Request('http://jaeger.train-ticket.svc:4318/v1/traces', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=5) as response:
        body = json.load(response)
        if body.get('partialSuccess', {}).get('rejectedSpans', 0):
            raise RuntimeError('trace rejected')


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global FAILED, STATE_CHANGE_ID
        if self.path != '/fault':
            return self.reply(404, {})
        payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        if DOWNSTREAM:
            with urlopen(Request(DOWNSTREAM+'/fault', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}), timeout=5) as response:
                response.read()
        else:
            before = FAILED
            FAILED = payload.get('failed') is True
            STATE_CHANGE_ID = uuid.uuid4().hex
            print(json.dumps({'event': 'state_transition', 'timestamp': time.time(),
                'pod': os.environ['POD_NAME'], 'pod_uid': os.environ['POD_UID'],
                'change_id': STATE_CHANGE_ID, 'request_method': 'POST', 'request_path': self.path,
                'field': 'FAILED', 'before': before, 'after': FAILED}), flush=True)
        self.reply(200, {'configured': True})

    def do_GET(self):
        if self.path == '/health':
            return self.reply(200, {'status':'ok'})
        started = time.time_ns()
        context = self.headers.get('traceparent', '').split('-')
        trace_id = context[1] if len(context) == 4 else uuid.uuid4().hex
        parent = context[2] if len(context) == 4 else None
        span_id = uuid.uuid4().hex[:16]
        code = 503 if FAILED else 200
        dependency = None
        client_span = None
        if DOWNSTREAM:
            client_id = uuid.uuid4().hex[:16] if os.environ.get('SPLIT_SPANS') == '1' else span_id
            client_started = time.time_ns()
            req = Request(DOWNSTREAM+'/probe', headers={'traceparent':f'00-{trace_id}-{client_id}-01'})
            try:
                response = urlopen(req, timeout=15)
            except HTTPError as exc:
                response = exc
            with response:
                code = response.code
                dependency = json.load(response)
            if client_id != span_id:
                client_span = {'traceId':trace_id, 'spanId':client_id, 'parentSpanId':span_id,
                               'name':ROLE+' downstream HTTP', 'kind':3,
                               'startTimeUnixNano':str(client_started), 'endTimeUnixNano':str(time.time_ns())}
        ended = time.time_ns()
        span = {'traceId':trace_id, 'spanId':span_id, 'name':ROLE+' GET /probe', 'kind':2,
                'startTimeUnixNano':str(started), 'endTimeUnixNano':str(ended),
                'status':{'code':2 if code >= 500 else 1},
                'attributes':[{'key':'error','value':{'boolValue':code>=500}},
                              {'key':'http.status_code','value':{'intValue':str(code)}}]}
        if parent:
            span['parentSpanId'] = parent
        if client_span:
            client_span.update(status=span['status'], attributes=span['attributes'])
            transmit(client_span)
        transmit(span)
        payload = {'pod':os.environ['POD_NAME'], 'code':code, 'start_ns':started, 'end_ns':ended,
                   'trace_id':trace_id, 'span_id':span_id, 'dependency':dependency,
                   'state': {'FAILED': FAILED, 'change_id': STATE_CHANGE_ID}}
        print(json.dumps(payload), flush=True)
        self.reply(code, payload)


HTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
