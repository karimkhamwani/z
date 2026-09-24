"""Local dashboard for the paper bot (no extra dependencies).

    .venv/bin/python dashboard.py                 # http://127.0.0.1:8765
    .venv/bin/python dashboard.py --port 9000 --data data

Run it next to the bot; it only reads data/status.json, data/portfolio.json and data/paper.db.
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bot.analytics import load, market_snapshots

HERE = Path(__file__).parent
PAGE = HERE / "dashboard" / "index.html"


class Cache:
    def __init__(self, ttl: float):
        self.ttl, self.t, self.v, self.lock = ttl, 0.0, None, threading.Lock()

    def get(self, fn):
        with self.lock:
            if time.time() - self.t > self.ttl:
                self.v, self.t = fn(), time.time()
            return self.v


def make_handler(data: Path):
    perf_cache = Cache(3.0)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(200, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            try:
                if u.path in ("/", "/index.html"):
                    self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                elif u.path == "/api/live":
                    p = data / "status.json"
                    if p.exists():
                        st = json.loads(p.read_text())
                        st["age_s"] = time.time() - st.get("ts", 0)
                        self._json(st)
                    else:
                        self._json({"missing": True})
                elif u.path == "/api/perf":
                    self._json(perf_cache.get(lambda: load(data)))
                elif u.path == "/api/market":
                    slug = parse_qs(u.query).get("slug", [""])[0]
                    self._json(market_snapshots(data, slug))
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as e:  # keep the dashboard up even if a read races the bot's writes
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(Path(a.data)))
    print(f"Dashboard: http://{a.host}:{a.port}   (data: {Path(a.data).resolve()})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
