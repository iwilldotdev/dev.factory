"""A tiny local server for env tests: `fake_server.py <port>`; serves the cwd and a few JSON routes. Behaviour comes from env vars."""
import http.server
import json
import mimetypes
import os
import sys
import time

port = int(sys.argv[1])
if os.environ.get("FAKE_DIE") or os.path.exists("FAKE_DIE_FILE"):
    print("fake server dying on purpose", flush=True)
    sys.exit(7)
time.sleep(float(os.environ.get("FAKE_DELAY", "0")))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("request", fmt % args, flush=True)

    def reply(self, status, body=b"", ctype="text/plain", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, obj):
        self.reply(status, json.dumps(obj).encode(), "application/json")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self.json(200, {"total": body.get("amount", 0) - (1 if os.environ.get("FAKE_BUG") else 0), "echo": body})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self.reply(int(os.environ.get("FAKE_HEALTH_STATUS", "200")), b"ok")
        elif path == "/whoami":
            self.json(200, {"cwd": os.getcwd().replace("\\", "/"), "prefix": os.environ.get("FACTORY_DATA_PREFIX"), "pid": os.getpid()})
        elif path == "/api/overdue":
            self.json(200, {"items": [{"status": "overdue", "id": 1}, {"status": "overdue", "id": 2}], "secret_note": "never-quote-this-body"})
        elif path == "/boom":
            self.reply(500, b"SSL Authentication Error: the database is degraded")
        elif path == "/maybe":
            flag = os.environ.get("FAKE_FLAG_FILE")
            degraded = bool(flag) and os.path.exists(flag)
            self.reply(500 if degraded else 200, b"SSL Authentication Error: degraded" if degraded else b"fine")
        elif path == "/redir":
            self.reply(302, b"", headers={"Location": "/health"})
        elif path == "/auth":
            ok = self.headers.get("X-Token") == "tok-123"
            self.json(200 if ok else 401, {"user": "qa"} if ok else {"error": "no token"})
        elif path == "/headers":
            self.json(200, {k.lower(): v for k, v in self.headers.items()})
        elif os.path.isfile(path.lstrip("/") or "index.html"):
            name = path.lstrip("/") or "index.html"
            self.reply(200, open(name, "rb").read(), mimetypes.guess_type(name)[0] or "application/octet-stream")
        else:
            self.reply(404, b"not found")


if os.environ.get("FAKE_NO_LISTEN"):
    time.sleep(600)
server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
print("fake server listening on", port, "in", os.getcwd(), flush=True)
server.serve_forever()
