"""Read-only HTTP access to the local newsdesk through a reverse SSH tunnel.

Run on the designated forwarding host. The SSH tunnel terminates on that
host's loopback port; no database, SSH key or application credential is copied.
"""

import argparse
import gzip
import http.client
import json
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlencode, urlsplit


STATIC_PATHS = {
    "/", "/index.html", "/favicon.svg", "/styles.css", "/app.js", "/contracts.js",
    "/benchmark.js", "/news-factor.js", "/source-maintenance.js", "/source-coverage.js",
    "/source-archive-coverage.js", "/source-coverage.html", "/source-archive-coverage.html",
    "/trading-return.js",
}
OIL_PREFIX = "/oil/"
FUTUREX_PREFIX = "/futurex/"
FUTUREX_FILES = {"", "index.html", "styles.css", "app.js", "data.js", "data.json"}
FUTUREX_API_PATHS = {"api/finance-data-lake"}
ANALYSIS_PREFIX = "/analysis/exp01/"
ANALYSIS_SNAPSHOT = re.compile(r"snapshot\.[a-f0-9]{64}\.json")
API_PATHS = {
    "/api/config", "/api/settings", "/api/research", "/api/source-coverage",
    "/api/sources/health", "/api/trading-return", "/api/trading-return/experiments",
    "/api/trading-return/model-test", "/api/trading-return/env-test", "/api/trading-return/mcp-test",
    "/api/benchmark", "/api/benchmark/build", "/api/benchmark/questions", "/api/evaluations",
    "/api/market/contracts", "/api/news", "/api/export",
}
API_PATTERNS = (
    re.compile(r"/api/market/(?:wti|brent|sc)-\d{6}(?:/indicators)?"),
    re.compile(r"/api/news/[A-Za-z0-9_-]+"),
    re.compile(r"/api/evaluations/[A-Za-z0-9_-]+(?:/cases/[A-Za-z0-9_-]+/trace)?"),
    re.compile(r"/api/trading-return/experiments/[A-Za-z0-9_-]+"),
)
RESPONSE_HEADERS = {"content-type", "content-disposition", "cache-control", "last-modified", "etag", "x-content-type-options"}
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_JSON_RESPONSE_BYTES = 128 * 1024 * 1024
GZIP_MIN_BYTES = 1024


def oil_allowed_path(path):
    return (path in STATIC_PATHS or path in API_PATHS
            or any(pattern.fullmatch(path) for pattern in API_PATTERNS))


def oil_target(path):
    """Keep the prefixed oil entry within its existing read-only allowlist."""
    if not path.startswith(OIL_PREFIX):
        return None
    target = "/" + path[len(OIL_PREFIX):]
    return target if oil_allowed_path(target) else None


def futurex_target(path):
    """Expose the FutureX public bundle and its read-only data API."""
    if not path.startswith(FUTUREX_PREFIX):
        return None
    suffix = path[len(FUTUREX_PREFIX):]
    return "/" + suffix if suffix in FUTUREX_FILES or suffix in FUTUREX_API_PATHS else None


def allowed_path(path):
    return (path in {ANALYSIS_PREFIX.rstrip("/"), OIL_PREFIX.rstrip("/"), FUTUREX_PREFIX.rstrip("/")}
            or analysis_target(path) is not None or futurex_target(path) is not None
            or oil_target(path) is not None or oil_allowed_path(path))


def analysis_target(path):
    """Map only the compiled analysis bundle, never arbitrary local files."""
    if not path.startswith(ANALYSIS_PREFIX):
        return None
    suffix = path[len(ANALYSIS_PREFIX):]
    if suffix in ("", "index.html") or ANALYSIS_SNAPSHOT.fullmatch(suffix):
        return "/" + suffix
    return None


def accepts_gzip(header):
    """Honor an explicit gzip refusal even when wildcard coding is accepted."""
    codings = {}
    for entry in header.lower().split(","):
        parts = [part.strip() for part in entry.split(";")]
        if not parts[0]:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            if parameter.startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        codings[parts[0]] = quality
    return codings.get("gzip", codings.get("*", 0)) > 0


class ReadOnlyProxy(BaseHTTPRequestHandler):
    server_version = "OilSignalForward/1.0"
    protocol_version = "HTTP/1.1"

    def _json(self, status, data, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def _forward(self):
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment or not allowed_path(parsed.path):
                self._json(404, {"error": "页面或只读接口不存在"})
                return
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                self._json(400, {"error": "只读访问不接受请求正文"})
                return
            # Public viewers can read cached/current market data. A refresh
            # request must remain a local action rather than trigger upstream
            # fetches repeatedly through a public URL.
            query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "refresh"]
            query_string = "?" + urlencode(query) if query else ""
            redirects = {prefix.rstrip("/"): prefix for prefix in (ANALYSIS_PREFIX, OIL_PREFIX, FUTUREX_PREFIX)}
            if parsed.path in redirects:
                self.send_response(308)
                self.send_header("Location", redirects[parsed.path] + query_string)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return
            analysis_path = analysis_target(parsed.path)
            futurex_path = futurex_target(parsed.path)
            if analysis_path is not None:
                upstream_port = getattr(self.server, "analysis_upstream_port", 18778)
                backend_host = getattr(self.server, "analysis_backend_host", "127.0.0.1:4178")
                target = analysis_path + query_string
            elif futurex_path is not None:
                upstream_port = getattr(self.server, "futurex_upstream_port", 18806)
                backend_host = getattr(self.server, "futurex_backend_host", "127.0.0.1:8806")
                target = futurex_path + query_string
            else:
                upstream_port = self.server.upstream_port
                backend_host = self.server.backend_host
                target = (oil_target(parsed.path) or parsed.path) + query_string
        except ValueError:
            self._json(400, {"error": "请求地址无效"})
            return
        upstream = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=60)
        try:
            upstream.request(self.command, target, headers={
                "Host": backend_host,
                "Accept": self.headers.get("Accept", "*/*"),
                "Connection": "close",
            })
            response = upstream.getresponse()
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            is_json = content_type == "application/json" or content_type.endswith("+json")
            limit = MAX_JSON_RESPONSE_BYTES if is_json else MAX_RESPONSE_BYTES
            declared_length = response.getheader("Content-Length")
            if self.command != "HEAD" and declared_length and declared_length.isdigit() and int(declared_length) > limit:
                self._json(502, {"error": "上游响应超过转发限制"})
                return
            body = response.read(limit + 1)
            if len(body) > limit:
                self._json(502, {"error": "上游响应超过转发限制"})
                return
            encoded = response.getheader("Content-Encoding")
            if self.command != "HEAD" and is_json and not encoded and len(body) >= GZIP_MIN_BYTES and accepts_gzip(self.headers.get("Accept-Encoding", "")):
                compressed = gzip.compress(body, compresslevel=1, mtime=0)
                if len(compressed) < len(body):
                    body = compressed
                    encoded = "gzip"
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in RESPONSE_HEADERS:
                    self.send_header(key, value)
            if encoded:
                self.send_header("Content-Encoding", encoded)
            if is_json:
                upstream_vary = response.getheader("Vary", "")
                self.send_header("Vary", upstream_vary + ", Accept-Encoding" if upstream_vary else "Accept-Encoding")
            self.send_header("Content-Length", response.getheader("Content-Length", "0") if self.command == "HEAD" else str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                self.wfile.write(body)
        except (OSError, http.client.HTTPException, socket.timeout):
            page_name = "FutureX 工作台" if futurex_path is not None else "新闻证据分析页" if analysis_path is not None else "本机研究台"
            self._json(502, {"error": page_name + "暂时未连接，请保持本机服务和SSH转发运行"})
        finally:
            upstream.close()

    def do_GET(self):
        self._forward()

    def do_HEAD(self):
        self._forward()

    def _read_only(self):
        self._json(405, {"error": "此访问入口仅支持查看；请在本机页面编辑或运行更新"}, {"Allow": "GET, HEAD"})

    do_POST = _read_only
    do_PUT = _read_only
    do_PATCH = _read_only
    do_DELETE = _read_only
    do_OPTIONS = _read_only
    do_CONNECT = _read_only
    do_TRACE = _read_only


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--upstream-port", type=int, default=18765)
    parser.add_argument("--backend-host", default="127.0.0.1:8765")
    parser.add_argument("--analysis-upstream-port", type=int, default=18778)
    parser.add_argument("--analysis-backend-host", default="127.0.0.1:4178")
    parser.add_argument("--futurex-upstream-port", type=int, default=18806)
    parser.add_argument("--futurex-backend-host", default="127.0.0.1:8806")
    args = parser.parse_args()
    if any(not 1 <= port <= 65535 for port in (args.port, args.upstream_port, args.analysis_upstream_port, args.futurex_upstream_port)):
        parser.error("端口必须在1至65535之间")
    if any(not re.fullmatch(r"(?:127\.0\.0\.1|localhost):\d{1,5}", host)
           or not 1 <= int(host.rsplit(":", 1)[1]) <= 65535
           for host in (args.backend_host, args.analysis_backend_host, args.futurex_backend_host)):
        parser.error("后端Host必须是本机地址和端口")
    server = ThreadingHTTPServer((args.bind, args.port), ReadOnlyProxy)
    server.daemon_threads = True
    server.upstream_port = args.upstream_port
    server.backend_host = args.backend_host
    server.analysis_upstream_port = args.analysis_upstream_port
    server.analysis_backend_host = args.analysis_backend_host
    server.futurex_upstream_port = args.futurex_upstream_port
    server.futurex_backend_host = args.futurex_backend_host
    print("Read-only newsdesk forward listening on {}:{}".format(args.bind, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
