import http.server
import json
import threading
import time


class Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    log_requests: list = []

    def log_message(self, *a):
        pass

    def _serve(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        type(self).log_requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
        fn = type(self).routes.get(self.path.split("?")[0])
        if fn is None:
            self.send_response(404); self.end_headers(); return
        res = fn(self, body)
        code, headers, payload = res
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve


def start_server(routes):
    cls = type("H", (Handler,), {"routes": routes, "log_requests": []})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, cls, f"http://127.0.0.1:{srv.server_address[1]}"


def jresp(obj, code=200):
    return code, {"Content-Type": "application/json"}, json.dumps(obj).encode()


def prom_vector(value):
    return jresp({"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [time.time(), str(value)]}]}})


def prom_empty():
    return jresp({"status": "success", "data": {"resultType": "vector", "result": []}})
