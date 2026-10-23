"""Disposable HTTP services exposing measured request duration and actual status."""
import os
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url=urlparse(self.path)
        if url.path=='/probe':
            args=parse_qs(url.query);delay=min(1000,max(0,int(args.get('delay_ms',['0'])[0])))
            fail=args.get('fail',['0'])[0]=='1'
            start=time.perf_counter();time.sleep(delay/1000)
            with self.server.lock:
                self.server.duration_ms=(time.perf_counter()-start)*1000
                self.server.failed=int(fail);self.server.requests+=1
            self.send_response(503 if fail else 200);self.end_headers();self.wfile.write(b'probe response\n')
        elif url.path=='/metrics':
            ns=os.environ['PROBE_NAMESPACE']
            with self.server.lock:
                body=(f'# TYPE agenticsre_probe_duration_ms gauge\nagenticsre_probe_duration_ms{{namespace="{ns}"}} {self.server.duration_ms}\n'
                      f'# TYPE agenticsre_probe_last_request_failed gauge\nagenticsre_probe_last_request_failed{{namespace="{ns}"}} {self.server.failed}\n'
                      f'# TYPE agenticsre_probe_requests_total counter\nagenticsre_probe_requests_total{{namespace="{ns}"}} {self.server.requests}\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/plain; version=0.0.4');self.end_headers();self.wfile.write(body)
        else:self.send_error(404)
    def log_message(self,*args):pass

if __name__=='__main__':
    for port in (18101,18102,18103):
        server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
        server.lock=threading.Lock();server.duration_ms=0;server.failed=0;server.requests=0
        threading.Thread(target=server.serve_forever,daemon=True).start()
    threading.Event().wait()
