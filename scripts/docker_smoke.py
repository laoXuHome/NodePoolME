#!/usr/bin/env python3
"""In-container acceptance smoke test for the NodePoolME Docker image.

It intentionally uses only the standard library so it can run inside the
runtime image without extra test dependencies.  It starts two local helpers
(a subscription/body HTTP server and a fake HTTP proxy), creates an airport,
fetches and caches one node, exercises Mihomo detection, business-group
detection, publishing, backup verification and download speed, and prints a
single JSON result.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = os.environ.get("NODEPOOL_SMOKE_BASE", "http://127.0.0.1:8080")
DB = Path(os.environ.get("NODEPOOL_DATA_DIR", "/data")) / "nodepool.db"
BACKUP_DIR = Path(os.environ.get("NODEPOOL_BACKUP_DIR", "/backup"))
PASSWORD = os.environ.get("NODEPOOL_ADMIN_PASSWORD", "nodepool")
SUB_PORT = 18099
PROXY_PORT = 18102
NODE_PORT = PROXY_PORT
SPEED_TOTAL = 8 * 1024 * 1024
checks: dict[str, object] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class CookieClient:
    def __init__(self) -> None:
        self.cookie = ""

    def call(self, method: str, path: str, payload=None, raw: bytes | None = None,
             content_type: str | None = None) -> tuple[int, dict | bytes]:
        data = raw
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
                set_cookie = resp.headers.get("Set-Cookie")
                if set_cookie:
                    self.cookie = set_cookie.split(";", 1)[0]
                ctype = resp.headers.get("Content-Type", "")
                return resp.status, (json.loads(body) if "json" in ctype else body)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            ctype = exc.headers.get("Content-Type", "")
            return exc.code, (json.loads(body) if "json" in ctype else body)


def http_server() -> ThreadingHTTPServer:
    sub_yaml = f"""proxies:\n  - name: Smoke Node HK\n    type: http\n    server: 127.0.0.1\n    port: {NODE_PORT}\n    udp: true\n""".encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/sub.yaml"):
                body = sub_yaml
                self.send_response(200)
                self.send_header("Content-Type", "text/yaml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/speed.bin"):
                total = SPEED_TOTAL
                rng = self.headers.get("Range", "")
                start, end = 0, total - 1
                if rng.startswith("bytes=") and "-" in rng:
                    a, _, b = rng[6:].partition("-")
                    start = int(a or 0)
                    end = min(total - 1, int(b or total - 1))
                body = b"S" * max(0, end - start + 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = b"SMOKE"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", SUB_PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ProxyHandler(socketserver.StreamRequestHandler):
    def _headers(self) -> dict[str, str]:
        out = {}
        while True:
            line = self.rfile.readline().decode("latin1")
            if not line or not line.strip():
                break
            if ":" in line:
                key, value = line.split(":", 1)
                out[key.strip().lower()] = value.strip()
        return out

    def _respond(self, headers: dict[str, str]):
        rng = headers.get("range", "")
        if rng.startswith("bytes=") and "-" in rng:
            a, _, b = rng[6:].partition("-")
            start = int(a or 0)
            end = min(SPEED_TOTAL - 1, int(b or SPEED_TOTAL - 1))
            length = max(0, end - start + 1)
            self.wfile.write(
                (f"HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {start}-{end}/"
                 f"{SPEED_TOTAL}\r\nContent-Length: {length}\r\nConnection: close\r\n\r\n").encode())
            chunk = b"S" * 65536
            left = length
            while left > 0:
                piece = chunk[:left]
                self.wfile.write(piece)
                left -= len(piece)
            return
        body = b"SMOKE"
        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                         b"Content-Length: 5\r\nConnection: close\r\n\r\n" + body)

    def handle(self):
        try:
            line = self.rfile.readline().decode("latin1").strip()
            if not line:
                return
            headers = self._headers()
            if line.upper().startswith("CONNECT"):
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.wfile.flush()
                line = self.rfile.readline().decode("latin1").strip()
                if not line:
                    return
                headers = self._headers()
            self._respond(headers)
        except Exception:
            return


def start_proxy() -> ProxyServer:
    server = ProxyServer(("127.0.0.1", PROXY_PORT), ProxyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def db_ro() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def main() -> int:
    ok = True
    httpd = http_server()
    proxy = start_proxy()
    c = CookieClient()
    try:
        status, health = c.call("GET", "/healthz")
        checks["healthz"] = {"status": status, "body": health}
        ok &= status == 200 and isinstance(health, dict) and health.get("engine") == "mihomo"

        status, login = c.call("POST", "/api/auth/login", {"password": PASSWORD})
        checks["login"] = {"status": status, "body": login}
        ok &= status == 200

        status, airport = c.call("POST", "/api/airports", {
            "name": "Smoke Airport",
            "url": f"http://127.0.0.1:{SUB_PORT}/sub.yaml",
            "enabled": True,
            "interval_minutes": 0,
            "filter_keywords": [],
        })
        checks["create_airport"] = {"status": status, "body": airport}
        ok &= status == 200 and isinstance(airport, dict)
        airport_id = airport.get("id") if isinstance(airport, dict) else None

        status, fetched = c.call("POST", f"/api/airports/{airport_id}/fetch")
        checks["fetch_cache"] = {"status": status, "body": fetched}
        ok &= status == 200 and isinstance(fetched, dict) and fetched.get("total_count") == 1

        status, nodes_payload = c.call("GET", "/api/nodes")
        nodes = nodes_payload.get("items") if isinstance(nodes_payload, dict) else None
        checks["node_pool"] = {"status": status, "count": len(nodes) if isinstance(nodes, list) else None}
        ok &= status == 200 and isinstance(nodes, list) and len(nodes) >= 1
        node = nodes[0] if isinstance(nodes, list) and nodes else {}
        node_id = node.get("id")

        status, group = c.call("POST", "/api/groups", {
            "name": "Smoke business", "ident": "SMK",
            "url": "http://smoke.local/business", "method": "GET",
            "timeout": 8, "expect_status": "200-399", "expect_body": "SMOKE",
            "max_latency": 0, "interval_minutes": 30, "enabled": True, "sort_order": 1,
        })
        checks["create_business_group"] = {"status": status, "body": group}
        ok &= status == 200 and isinstance(group, dict)
        group_id = group.get("id") if isinstance(group, dict) else None

        status, tested = c.call("POST", f"/api/nodes/{node_id}/test", {
            "group_ids": [group_id], "mode": "all",
        })
        checks["detect_mihomo_and_business"] = {"status": status, "body": tested}
        results = tested.get("results") if isinstance(tested, dict) else None
        ok &= status == 200 and isinstance(tested, dict) and tested.get("ok") is True
        ok &= isinstance(tested.get("latency"), dict) and tested["latency"].get("ok") is True
        ok &= isinstance(results, list) and len(results) == 1 and results[0].get("ok") is True

        status, published = c.call("POST", "/api/subscription/rebuild")
        checks["publish"] = {"status": status, "body": published}
        ok &= status == 200 and isinstance(published, dict) and published.get("nodes", 0) >= 1

        status, sub = c.call("GET", "/api/subscription")
        token = (sub.get("settings") or {}).get("sub_token") if isinstance(sub, dict) else None
        ok &= status == 200 and bool(token)
        status, body = c.call("GET", f"/sub/all/{token}/base64")
        decoded = base64.b64decode(body).decode("utf-8", "replace") if status == 200 else ""
        checks["subscription_output"] = {
            "status": status, "contains_node": "Smoke Node HK" in decoded,
            "length": len(decoded),
        }
        ok &= status == 200 and "Smoke Node HK" in decoded

        status, backup = c.call("POST", "/api/system/backup")
        checks["backup"] = {"status": status, "body": backup}
        ok &= status == 200 and isinstance(backup, dict) and (BACKUP_DIR / backup.get("file", "")).exists()
        backup_file = BACKUP_DIR / backup["file"]
        report = subprocess.run(
            [sys.executable, "-m", "backend.database.cli", "verify", "--file", str(backup_file), "--json"],
            capture_output=True, text=True, cwd=os.environ.get("NODEPOOL_APP_DIR", "/app"), timeout=30,
        )
        checks["backup_cli_verify"] = {"returncode": report.returncode,
                                       "stdout": report.stdout.strip()[:2000],
                                       "stderr": report.stderr.strip()[:500]}
        ok &= report.returncode == 0

        status, backup_bytes = c.call("GET", "/api/system/backup/download")
        ok &= status == 200 and isinstance(backup_bytes, bytes) and len(backup_bytes) > 0
        boundary = "----nodepool-smoke-boundary"
        multipart = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="smoke.db"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + (backup_bytes if isinstance(backup_bytes, bytes) else b"") + (
            f"\r\n--{boundary}--\r\n"
        ).encode()
        status, restore = c.call("POST", "/api/system/restore", raw=multipart,
                                 content_type=f"multipart/form-data; boundary={boundary}")
        checks["restore_guard"] = {"status": status, "body": restore}
        ok &= status == 409 and isinstance(restore, dict) and \
            (restore.get("detail") or {}).get("code") == "manual_import_required"

        # Local speed sources: a real HTTPS source check is not possible offline.
        # The fake proxy answers Range requests, so the Mihomo SOCKS path and the
        # download budget/measurement code are still exercised end to end.
        stamp = now_iso()
        conn = sqlite3.connect(DB)
        conn.execute("DELETE FROM speed_sources WHERE name LIKE 'Smoke local%'")
        ids = []
        for i in (1, 2):
            cur = conn.execute(
                "INSERT INTO speed_sources (name,region,url,priority,enabled,health,range_ok,"
                "size_bytes,latency_ms,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"Smoke local {i}", "local", f"http://speed.local/{i}.bin", 0, 1, "ok", 1,
                 SPEED_TOTAL, 1, stamp, stamp),
            )
            ids.append(cur.lastrowid)
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('speed_primary_sources','1')")
        conn.commit()
        conn.close()
        c.call("PATCH", "/api/settings", {
            "speed_enabled": True,
            "speed_connections": 4, "speed_warmup_seconds": 0,
            "speed_sample_seconds": 3, "speed_total_timeout": 10,
            "speed_traffic_limit_mb": 20,
        })
        status, speed = c.call("POST", "/api/speed/test", {
            "node_id": node_id, "source_ids": ids,
        })
        checks["speed"] = {"status": status, "body": speed}
        ok &= status == 200 and isinstance(speed, dict) and speed.get("status") in ("ok", "partial")
        ok &= (speed.get("bytes_total") or 0) > 0 and speed.get("sources_ok", 0) >= 1

        conn = db_ro()
        rows = conn.execute("SELECT version,status,seq FROM schema_migrations ORDER BY seq").fetchall()
        tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        conn.close()
        checks["schema_migrations"] = {"rows": rows, "tables": tables}
        ok &= len(rows) >= 1 and rows[0][1] == "success"

        checks["result"] = "PASS" if ok else "FAIL"
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    finally:
        httpd.shutdown()
        proxy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
