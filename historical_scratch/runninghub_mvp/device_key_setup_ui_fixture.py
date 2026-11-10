"""Loopback-only synthetic UI fixture, no CNG, real login or cloud access."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\apps\processor\frontend\new")
status = {"state": "KEY_UNAVAILABLE", "code": "KEY_ACCESS_DENIED"}
next_result = {"state": "KEY_UNAVAILABLE", "code": "KEY_SETUP_CANCELLED", "detail": "已取消 Windows 权限确认"}
actions = []
ASSETS = {"device-authorization.html", "device-authorization.css", "device-authorization.js", "device-h3-recovery.js"}


class Handler(BaseHTTPRequestHandler):
    def respond(self, value, code=200, mime="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(value if isinstance(value, bytes) else json.dumps(value).encode())

    def do_GET(self):
        if self.path == "/__test/actions":
            return self.respond(actions)
        if self.path == "/api/new/device-authorization":
            return self.respond({"user_id": 7, **status})
        if self.path == "/api/new/device-authorization/waiting-jobs":
            return self.respond({"jobs": []})
        if self.path.startswith("/api/new/device-authorization/h3-waiting"):
            return self.respond({"schema": "runninghub.h3-authorization-recovery.v1", "batches": [], "next_cursor": None})
        name = "device-authorization.html" if self.path == "/app/new/device-authorization" else self.path.removeprefix("/app-static/new/")
        if name not in ASSETS:
            return self.respond({}, 404)
        mime = "text/html" if name.endswith(".html") else "text/css" if name.endswith(".css") else "text/javascript"
        return self.respond((ROOT / name).read_bytes(), mime=mime + "; charset=utf-8")

    def do_POST(self):
        global status, next_result
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/__test/scenario":
            status = payload["status"]
            next_result = payload.get("next_result", status)
            if payload.get("reset_actions"):
                actions.clear()
            return self.respond({"ok": True})
        if self.path not in {"/api/new/device-authorization/repair-key-access", "/api/new/device-authorization/refresh", "/api/new/device-authorization/apply", "/api/new/device-authorization/apply-software"}:
            return self.respond({}, 404)
        actions.append({"path": self.path, "payload": payload, "action_header": self.headers.get("X-Device-Authorization-Action")})
        if self.path.endswith(("/repair-key-access", "/apply", "/apply-software")):
            status = dict(next_result)
        return self.respond({"user_id": 7, **status}, 409 if status.get("code") else 200)

    def log_message(self, *_):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(f"Key setup UI: http://127.0.0.1:{server.server_port}/app/new/device-authorization", flush=True)
server.serve_forever()
