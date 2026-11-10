"""Temporary local UI fixture; no actual licensing backend or native keys."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time

ROOT = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\apps\processor\frontend\new")
state = "UNREGISTERED"


class Handler(BaseHTTPRequestHandler):
    def respond(self, data, content_type="application/json", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def status_payload(self):
        return json.dumps({"state": state, "user_id": 7,
            "thumbprint": "A" * 43 if state != "UNREGISTERED" else None,
            "device_id": "demo-device-250" if state == "ACTIVE" else None,
            "protection_report": "tpm" if state != "UNREGISTERED" else None,
            "exp": int(time.time()) + 1800 if state == "ACTIVE" else None,
        }).encode()

    def do_GET(self):
        if self.path == "/api/new/device-authorization":
            return self.respond(self.status_payload())
        allowed = {"/app/new/device-authorization": ("device-authorization.html", "text/html; charset=utf-8"),
                   "/app-static/new/device-h3-recovery.js": ("device-h3-recovery.js", "text/javascript; charset=utf-8"),
                   "/app-static/new/device-authorization.css": ("device-authorization.css", "text/css"),
                   "/app-static/new/device-authorization.js": ("device-authorization.js", "text/javascript; charset=utf-8")}
        if self.path not in allowed:
            return self.respond(b"{}", status=404)
        name, mime = allowed[self.path]
        self.respond((ROOT / name).read_bytes(), mime)

    def do_POST(self):
        global state
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/new/device-authorization/apply":
            state = "PENDING"
        elif self.path == "/api/new/device-authorization/refresh":
            state = "ACTIVE"
        else:
            return self.respond(b"{}", status=404)
        self.respond(self.status_payload())

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(f"Device UI fixture: http://127.0.0.1:{server.server_port}/app/new/device-authorization", flush=True)
server.serve_forever()
