#!/usr/bin/env python3
"""
A-Share Risk Monitor - local proxy server

push2his.eastmoney.com uses TLS fingerprinting; plain requests get disconnected (RemoteDisconnected).
This proxy uses curl_cffi to mimic a Chrome124 TLS fingerprint for push2his; other domains use requests.

Dependencies:
    pip install requests curl_cffi

Run: python proxy.py
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse
import urllib.request
import json
import sys

# ── requests (regular domains) ──────────────────────────────────────
try:
    import requests
    from requests.adapters import HTTPAdapter
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ── curl_cffi (push2his TLS fingerprint bypass) ───────────────────────
try:
    from curl_cffi import requests as cf_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

PORT = 8899

ALLOWED_HOSTS = [
    "hq.sinajs.cn",
    "money.finance.sina.com.cn",   # HV30 historical candles
    "push2.eastmoney.com",
    "82.push2.eastmoney.com",
    "datacenter-web.eastmoney.com",
    "push2his.eastmoney.com",
    "stock.gtimg.cn",
    "qt.gtimg.cn",
]

# push2his requires curl_cffi to mimic Chrome TLS; plain requests get RemoteDisconnected
CURL_CFFI_HOSTS = ["push2his.eastmoney.com"]

REFERER_MAP = {
    "sinajs.cn":     "https://finance.sina.com.cn/",
    "eastmoney.com": "https://quote.eastmoney.com/",
    "gtimg.cn":      "https://gu.qq.com/",
}

BROWSER_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-site",
}

# requests Session (regular domains)
session = None
if HAS_REQUESTS:
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=2)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def get_referer(url):
    for key, ref in REFERER_MAP.items():
        if key in url:
            return ref
    return "https://finance.sina.com.cn/"


def needs_curl_cffi(host):
    return any(host.endswith(h) for h in CURL_CFFI_HOSTS)


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[Proxy] {args[0]} {args[1]}", flush=True)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        # ── Semantic endpoints: serve data straight from the daily-updated arisk_data.json  ──
        # The dashboard uses these endpoints to check whether"is live"(detectEnv probes /pe) and reads today’s data;
        # the generic reverse-proxy version lacked these, so the dashboard kept falling back to static values.
        semantic = self._semantic(parsed.path)
        if semantic is not None:
            self._json(semantic)
            return

        if "url" not in params:
            self._error(400, "Missing ?url= parameter")
            return

        target_url = urllib.parse.unquote(params["url"][0])
        target_host = urllib.parse.urlparse(target_url).netloc

        if not any(target_host.endswith(h) for h in ALLOWED_HOSTS):
            self._error(403, f"Host not allowed: {target_host}")
            return

        try:
            headers = {**BROWSER_HEADERS, "Referer": get_referer(target_url)}

            if needs_curl_cffi(target_host):
                # curl_cffi: mimic Chrome124 TLS fingerprint to get past push2his anti-scraping
                if not HAS_CURL_CFFI:
                    self._error(503, "curl_cffi not installed. Run: pip install curl_cffi")
                    return
                resp = cf_requests.get(
                    target_url,
                    headers=headers,
                    impersonate="chrome124",
                    timeout=15,
                )
                data = resp.content
                content_type = resp.headers.get("Content-Type", "text/plain; charset=utf-8")

            elif HAS_REQUESTS and session:
                # requests: regular domains
                resp = session.get(target_url, headers=headers, timeout=15)
                data = resp.content
                content_type = resp.headers.get("Content-Type", "text/plain; charset=utf-8")

            else:
                # urllib fallback (imported at top level, no local name clash)
                req = urllib.request.Request(target_url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = r.read()
                    content_type = r.headers.get("Content-Type", "text/plain; charset=utf-8")

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        except Exception as e:
            print(f"[Proxy] Error: {target_url} → {e}", flush=True)
            self._error(502, str(e))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/mx":
            self._mx_proxy()
        else:
            self._error(404, "Not found")

    def _mx_proxy(self):
        import os
        api_key = os.environ.get("MX_APIKEY", "")
        if not api_key:
            self._error(503, "MX_APIKEY not set. Run: export MX_APIKEY=your_key")
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            mx_url = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"
            headers = {"Content-Type": "application/json", "apikey": api_key}

            if HAS_CURL_CFFI:
                resp = cf_requests.post(mx_url, content=body, headers=headers, timeout=30)
                data = resp.content
                content_type = resp.headers.get("Content-Type", "application/json")
            elif HAS_REQUESTS and session:
                resp = session.post(mx_url, data=body, headers=headers, timeout=30)
                data = resp.content
                content_type = resp.headers.get("Content-Type", "application/json")
            else:
                req = urllib.request.Request(mx_url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                    content_type = r.headers.get("Content-Type", "application/json")

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            print(f"[Proxy] MX Error: {e}", flush=True)
            self._error(502, str(e))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _error(self, code, msg):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}).encode())

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _semantic(self, path):
        """Dashboard semantic endpoints → read the daily-updated file in this directory: arisk_data.json. 
        Returns None if not a semantic endpoint (handed back to the generic ?url= reverse proxy)."""
        import os
        routes = {"/pe", "/bond", "/prebuilt", "/sectors", "/margin", "/sf", "/fund"}
        if path not in routes:
            return None
        data_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arisk_data.json")
        try:
            with open(data_file, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        if path == "/pe":
            return {"pe": d.get("pe_300"), "pb": None}
        if path == "/bond":
            b = d.get("bond10y") or {}
            return {"yield": b.get("latest"), "hist": b.get("hist", [])}
        if path == "/prebuilt":
            return d
        if path == "/sectors":
            return d.get("sector_live", [])
        if path == "/margin":
            return (d.get("margin") or {}).get("monthly", [])
        if path == "/sf":
            return d.get("m2_monthly", [])
        if path == "/fund":
            return d.get("fund_issuance", [])
        return None


if __name__ == "__main__":
    req_status  = "installed" if HAS_REQUESTS  else "not installed → pip install requests"
    cffi_status = "installed" if HAS_CURL_CFFI else "not installed → pip install curl_cffi  ← required for push2his candles"

    import os
    mx_status = "configured" if os.environ.get("MX_APIKEY") else "not configured → export MX_APIKEY=your_key  ← required for Miaoxiang API"
    print(f"""
╔══════════════════════════════════════════════════════╗
║   A-Share Risk Monitor proxy  v3                      ║
║   Port  : {PORT}                                   ║
║   requests  : {req_status}
║   curl_cffi : {cffi_status}
║   MX_APIKEY : {mx_status}
║                                                      ║
║   GET  /?url=...  → reverse proxy (CORS/TLS bypass)         ║
║   POST /mx        → Eastmoney Miaoxiang API proxy                   ║
║   push2his → curl_cffi Chrome124 TLS (anti-scraping bypass)      ║
╚══════════════════════════════════════════════════════╝
""", flush=True)

    if not HAS_CURL_CFFI:
        print("[Proxy] ⚠  sector candles / bond candles / option IV history will all return 502. Install first: curl_cffi\n", flush=True)

    server = HTTPServer(("localhost", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Proxy] Stopped", flush=True)
        sys.exit(0)
