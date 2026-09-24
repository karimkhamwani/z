"""Local dashboard for the paper bot (no extra dependencies).

    python manage.py dashboard                    # http://127.0.0.1:8766
    python manage.py dashboard --port 9000 --data path/to/data

Run it next to the bot; it only reads data/status.json, data/portfolio.json and data/paper.db.
"""
import argparse
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bot.analytics import fills_csv, fills_page, load, market_snapshots, orders_page, settlements_page

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
                        st = json.loads(p.read_text(encoding="utf-8"))
                        st["age_s"] = time.time() - st.get("ts", 0)
                        self._json(st)
                    else:
                        self._json({"missing": True})
                elif u.path == "/api/perf":
                    self._json(perf_cache.get(lambda: load(data)))
                elif u.path == "/api/fills":
                    q = parse_qs(u.query)
                    self._json(fills_page(data, offset=max(0, int(q.get("offset", ["0"])[0])),
                                          limit=min(500, max(1, int(q.get("limit", ["50"])[0]))),
                                          result=q.get("result", ["all"])[0], side=q.get("side", ["all"])[0]))
                elif u.path == "/api/settlements":
                    q = parse_qs(u.query)
                    self._json(settlements_page(data, offset=max(0, int(q.get("offset", ["0"])[0])),
                                                limit=min(500, max(1, int(q.get("limit", ["50"])[0]))),
                                                result=q.get("result", ["all"])[0]))
                elif u.path == "/api/orders":
                    q = parse_qs(u.query)
                    self._json(orders_page(data, offset=max(0, int(q.get("offset", ["0"])[0])),
                                           limit=min(500, max(1, int(q.get("limit", ["50"])[0])))))
                elif u.path == "/fills.csv":
                    body = fills_csv(data).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", f'attachment; filename="paper_fills_{time.strftime("%Y%m%d_%H%M")}.csv"')
                    self.end_headers()
                    self.wfile.write(body)
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
    ap.add_argument("--data", default=str(Path(__file__).resolve().parent / "data"))
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="don't open the dashboard in a browser")
    ap.add_argument("--live", action="store_true", help="show the live/shadow ledger (data_live/) instead of paper")
    a = ap.parse_args()
    if a.live:
        a.data = str(Path(__file__).resolve().parent / "data_live")
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(Path(a.data)))
    url = f"http://{a.host}:{a.port}"
    print(f"Dashboard: {url}   (data: {Path(a.data).resolve()})   Ctrl+C to stop")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
