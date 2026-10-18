"""ローカル開発サーバ。127.0.0.1だけで待ち受け、定期処理も同じプロセスで動かす。

使い方: python -m xronix.devserver [--port 8787] [--data-dir .xronix]
環境変数 XRONIX_X_MODE=mock(既定) / live
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .api import Api
from .app import App, build_local

WEB_DIR = os.environ.get("XRONIX_WEB_DIR") or os.path.join(os.path.dirname(__file__), "..", "..", "web")


def make_handler(api: Api):
    class H(BaseHTTPRequestHandler):
        server_version = "XroniX"

        def log_message(self, fmt, *args):  # アクセスログに本文・秘密を出さない
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _api(self, method: str):
            u = urllib.parse.urlparse(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            body = {}
            if method in ("POST", "PUT", "DELETE"):
                # 他サイトからの書き込みを防ぐ（DNSリバインディングとCSRF対策）
                if self.headers.get("X-Requested-With") != "xronix":
                    return self._send(403, b'{"error":"forbidden"}', "application/json")
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    try:
                        body = json.loads(self.rfile.read(n))
                    except ValueError:
                        return self._send(400, '{"error":"JSONが不正です"}'.encode(), "application/json")
            status, payload, headers = api.handle(method, u.path, query, body, {k.lower(): v for k, v in self.headers.items()})
            self._send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8", headers)

        def _static(self):
            path = urllib.parse.urlparse(self.path).path
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.normpath(os.path.join(WEB_DIR, rel))
            if not full.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            with open(full, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            self._send(200, data, ctype + ("; charset=utf-8" if ctype.startswith("text/") or "javascript" in ctype else ""))

        def _dispatch(self, method: str):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            if self.path.startswith("/api/"):
                return self._api(method)
            if method == "GET":
                return self._static()
            self._send(405, b"", "text/plain")

        def do_GET(self): self._dispatch("GET")
        def do_POST(self): self._dispatch("POST")
        def do_PUT(self): self._dispatch("PUT")
        def do_DELETE(self): self._dispatch("DELETE")

    return H


def start_ticker(app: App, interval: int = 5) -> threading.Event:
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            try:
                app.jobs.tick()
                app.sendq.tick()
            except Exception as e:  # noqa: BLE001
                app.logger.error("job", f"定期処理で例外: {type(e).__name__}")

    threading.Thread(target=loop, daemon=True, name="ticker").start()
    return stop


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--data-dir", default=".xronix")
    a = p.parse_args()
    app = build_local(a.data_dir)
    start_ticker(app)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(Api(app)))
    print(f"XroniX: http://127.0.0.1:{a.port}/  （Xモード: {app.x.mode}）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
