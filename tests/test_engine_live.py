"""Live engine test: local HTTP target + minimal SOCKS5 proxy + mihomo core.

Proves the whole path: node -> mihomo listener -> proxy -> HTTP status/body observed.
"""
import asyncio
import os
import socket
import socketserver
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                if "/tests/" in os.path.abspath(__file__) else ".")
os.environ.setdefault("NODEPOOL_DATA_DIR", "/tmp/npdata_engine")
os.environ.setdefault("NODEPOOL_TEST_ENGINE", "mihomo")
os.environ.setdefault("NODEPOOL_MIHOMO_BIN", "/tmp/mihomo")
os.environ.setdefault("NODEPOOL_MIHOMO_API", "http://127.0.0.1:19090")
os.environ.setdefault("NODEPOOL_MIHOMO_BASE_PORT", "32000")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/generate_204"):
            self.send_response(204)
            self.end_headers()
        elif self.path == "/body":
            body = b"nodepool-ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(403)
            self.end_headers()

    def log_message(self, *args):
        pass


class Socks5(socketserver.BaseRequestHandler):
    """RFC1928 CONNECT only, no auth."""

    def handle(self):
        try:
            data = self.request.recv(262)
            if not data or data[0:1] != b"\x05":
                return
            self.request.sendall(b"\x05\x00")
            req = self.request.recv(4)
            atyp = req[3]
            if atyp == 1:
                addr = socket.inet_ntoa(self.request.recv(4))
            elif atyp == 3:
                n = self.request.recv(1)[0]
                addr = self.request.recv(n).decode()
            else:
                return
            port = struct.unpack("!H", self.request.recv(2))[0]
            upstream = socket.create_connection((addr, port), timeout=5)
            self.request.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            self._pump(self.request, upstream)
        except Exception:
            pass
        finally:
            try:
                self.request.close()
            except Exception:
                pass

    @staticmethod
    def _pump(a, b):
        import select
        while True:
            r, _, _ = select.select([a, b], [], [], 30)
            if not r:
                return
            for sock in r:
                data = sock.recv(4096)
                if not data:
                    return
                (b if sock is a else a).sendall(data)


def run_servers():
    httpd = HTTPServer(("127.0.0.1", 18181), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    socks = socketserver.ThreadingTCPServer(("127.0.0.1", 18182), Socks5)
    socks.daemon_threads = True
    threading.Thread(target=socks.serve_forever, daemon=True).start()


async def main():
    run_servers()
    from backend.database import db
    from backend.testers.engine import MihomoEngine
    db.init_db()
    node = {"id": 1, "fingerprint": "fp-test", "protocol": "socks5", "server": "127.0.0.1",
            "port": 18182, "raw_name": "local", "params": {}}
    engine = MihomoEngine()
    await engine.start()
    try:
        await engine.prepare([node])
        ok_group = {"id": 1, "name": "Y", "url": "http://127.0.0.1:18181/generate_204",
                    "method": "GET", "timeout": 8, "expect_status": "204", "expect_body": ""}
        res = await engine.test(node, ok_group)
        print("204 test ->", res)
        assert res.ok and res.latency_ms is not None, res

        body_group = {"id": 2, "name": "G", "url": "http://127.0.0.1:18181/body", "method": "GET",
                      "timeout": 8, "expect_status": "200", "expect_body": "nodepool-ok"}
        res = await engine.test(node, body_group)
        print("body match ->", res)
        assert res.ok, res

        fail_group = {"id": 3, "name": "TG", "url": "http://127.0.0.1:18181/body", "method": "GET",
                      "timeout": 8, "expect_status": "204", "expect_body": ""}
        res = await engine.test(node, fail_group)
        print("status mismatch ->", res)
        assert not res.ok and res.error_type == "status", res
        print("LIVE ENGINE OK")
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
