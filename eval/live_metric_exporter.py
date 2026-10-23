"""Disposable exporter: count actual probe HTTP requests on two independent listeners."""
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path=='/probe': self.server.count+=1
        elif self.path=='/reset': self.server.count=0
        elif self.path!='/metrics':self.send_error(404);return
        body=(f'# TYPE agenticsre_probe_requests_total counter\nagenticsre_probe_requests_total{{namespace="{os.environ["PROBE_NAMESPACE"]}"}} {self.server.count}\n').encode()
        self.send_response(200);self.send_header('Content-Type','text/plain; version=0.0.4');self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass

for port in (18101,18102):
    server=HTTPServer(('127.0.0.1',port),Handler);server.count=0
    threading.Thread(target=server.serve_forever,daemon=True).start()
threading.Event().wait()
