"""Large news JSON remains readable through the read-only forwarding entry."""

import gzip
import http.client
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch

from newsdesk import public_proxy


class Upstream(BaseHTTPRequestHandler):
    calls = []
    body = b'{"items":[{"body":"' + b"a" * (33 * 1024 * 1024) + b'"}],"total":1}'

    def do_GET(self):
        type(self).calls.append((self.command, self.path, self.headers.get("Host")))
        body = self.body if self.path.startswith("/api/news") else b"small static asset"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8" if self.path.startswith("/api/news") else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The proxy can reject a declared over-limit body immediately.
                pass

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


class QuietProxy(public_proxy.ReadOnlyProxy):
    def log_message(self, *args):
        pass


class AnalysisUpstream(BaseHTTPRequestHandler):
    calls = []
    snapshot_name = "snapshot." + "a" * 64 + ".json"
    html = b"<!doctype html><title>Exp01 analysis</title>"
    snapshot = b'{"queries":{"evidence":{"rows":[]}},"title":"Exp01"}'

    def do_GET(self):
        type(self).calls.append((self.command, self.path, self.headers.get("Host")))
        is_snapshot = self.path.split("?", 1)[0] == "/" + self.snapshot_name
        body = self.snapshot if is_snapshot else self.html
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if is_snapshot else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


class FuturexUpstream(BaseHTTPRequestHandler):
    calls = []
    assets = {
        "/": ("text/html; charset=utf-8", b"<!doctype html><title>FutureX Benchmark</title>"),
        "/index.html": ("text/html; charset=utf-8", b"<!doctype html><title>FutureX Benchmark</title>"),
        "/styles.css": ("text/css", b".futurex { color: teal; }"),
        "/app.js": ("text/javascript", b"window.futurexLoaded = true;"),
        "/data.js": ("text/javascript", b"window.FUTUREX_DATA = {periods: []};"),
        "/data.json": ("application/json", b'{"periods":[]}'),
        "/api/finance-data-lake": ("application/json", b'{"articles":[],"total":0}'),
    }

    def do_GET(self):
        type(self).calls.append((self.command, self.path, self.headers.get("Host")))
        content_type, body = self.assets[self.path.split("?", 1)[0]]
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


class PublicProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.analysis_upstream = ThreadingHTTPServer(("127.0.0.1", 0), AnalysisUpstream)
        cls.futurex_upstream = ThreadingHTTPServer(("127.0.0.1", 0), FuturexUpstream)
        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), QuietProxy)
        cls.proxy.upstream_port = cls.upstream.server_port
        cls.proxy.backend_host = "127.0.0.1:8765"
        cls.proxy.analysis_upstream_port = cls.analysis_upstream.server_port
        cls.proxy.analysis_backend_host = "127.0.0.1:4178"
        cls.proxy.futurex_upstream_port = cls.futurex_upstream.server_port
        cls.proxy.futurex_backend_host = "127.0.0.1:8806"
        for server in (cls.upstream, cls.analysis_upstream, cls.futurex_upstream, cls.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.proxy, cls.futurex_upstream, cls.analysis_upstream, cls.upstream):
            server.shutdown()
            server.server_close()

    def request(self, path, method="GET", encoding=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=10)
        headers = {"Accept-Encoding": encoding} if encoding is not None else {}
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_news_larger_than_old_32mib_limit_is_compressed_and_lossless(self):
        status, headers, body = self.request("/api/news", encoding="br, gzip")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(headers.get("Vary"), "Accept-Encoding")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertLess(len(body), len(Upstream.body) // 10)
        self.assertEqual(gzip.decompress(body), Upstream.body)
        self.assertEqual(Upstream.calls[-1][2], "127.0.0.1:8765")

    def test_clients_refusing_gzip_keep_identity_response(self):
        status, headers, body = self.request("/api/news", encoding="gzip;q=0, *;q=1")
        self.assertEqual(status, 200)
        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual(body, Upstream.body)

    def test_json_response_above_new_limit_still_fails_without_large_transfer(self):
        with patch.object(public_proxy, "MAX_JSON_RESPONSE_BYTES", 1024):
            status, _, body = self.request("/api/news", method="HEAD")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"")
            status, _, body = self.request("/api/news")
        self.assertEqual(status, 502)
        self.assertIn("上游响应超过转发限制", body.decode())

    def test_write_requests_still_rejected_without_reaching_backend(self):
        count = len(Upstream.calls)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, headers, _ = self.request("/api/settings", method=method)
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "GET, HEAD")
        self.assertEqual(len(Upstream.calls), count)

    def test_read_forward_still_strips_refresh_and_blocks_unknown_paths(self):
        self.request("/api/news?refresh=1&q=oil", method="HEAD")
        self.assertEqual(Upstream.calls[-1][1], "/api/news?q=oil")
        self.assertEqual(self.request("/../README.md")[0], 404)

    def test_analysis_root_forwards_to_its_own_upstream(self):
        workbench_calls = len(Upstream.calls)
        status, headers, body = self.request("/analysis/exp01/?view=1&tab=evidence")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertEqual(body, AnalysisUpstream.html)
        self.assertEqual(AnalysisUpstream.calls[-1][:2], ("GET", "/?view=1&tab=evidence"))
        self.assertEqual(len(Upstream.calls), workbench_calls)

    def test_analysis_head_retains_content_length_without_response_body(self):
        status, headers, body = self.request("/analysis/exp01/index.html", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(int(headers["Content-Length"]), len(AnalysisUpstream.html))
        self.assertEqual(body, b"")
        self.assertEqual(AnalysisUpstream.calls[-1][:2], ("HEAD", "/index.html"))

    def test_analysis_snapshot_uses_hashed_asset_name(self):
        path = "/analysis/exp01/" + AnalysisUpstream.snapshot_name
        status, headers, body = self.request(path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body, AnalysisUpstream.snapshot)
        self.assertEqual(AnalysisUpstream.calls[-1][1], "/" + AnalysisUpstream.snapshot_name)

    def test_analysis_missing_trailing_slash_redirect_preserves_view(self):
        calls = len(AnalysisUpstream.calls)
        status, headers, _ = self.request("/analysis/exp01?view=1&tab=evidence")
        self.assertIn(status, (301, 302, 307, 308))
        self.assertEqual(headers["Location"], "/analysis/exp01/?view=1&tab=evidence")
        self.assertEqual(len(AnalysisUpstream.calls), calls)

    def test_analysis_rejects_arbitrary_files_and_path_traversal(self):
        calls = len(AnalysisUpstream.calls)
        for path in (
            "/analysis/exp01/README.md", "/analysis/exp01/data-app-build.json",
            "/analysis/exp01/snapshot.short.json", "/analysis/exp01/../index.html",
            "/analysis/exp01/%2e%2e/index.html", "/analysis/exp01//index.html",
            "/analysis/exp01/_data/components/arbitrary", "/analysis/exp01/api/settings",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(len(AnalysisUpstream.calls), calls)

    def test_analysis_disconnect_does_not_break_workbench(self):
        # Reserve a loopback port without listening so connection refusal is
        # deterministic, without disturbing either running upstream.
        with socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            with patch.object(self.proxy, "analysis_upstream_port", unavailable.getsockname()[1]):
                self.assertEqual(self.request("/analysis/exp01/")[0], 502)
                self.assertEqual(self.request("/")[0], 200)

    def test_analysis_write_requests_never_reach_either_backend(self):
        workbench_calls = len(Upstream.calls)
        analysis_calls = len(AnalysisUpstream.calls)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, headers, _ = self.request("/analysis/exp01/", method=method)
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "GET, HEAD")
        self.assertEqual(len(Upstream.calls), workbench_calls)
        self.assertEqual(len(AnalysisUpstream.calls), analysis_calls)

    def test_oil_prefix_maps_assets_and_read_only_api_to_existing_backend(self):
        other_calls = (len(AnalysisUpstream.calls), len(FuturexUpstream.calls))
        for suffix in ("", "index.html", "styles.css", "app.js", "contracts.js", "benchmark.js", "favicon.svg"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.request("/oil/" + suffix)[0], 200)
                self.assertEqual(Upstream.calls[-1], ("GET", "/" + suffix, "127.0.0.1:8765"))
        self.assertEqual(self.request("/oil/api/news?refresh=1&q=oil", method="HEAD")[0], 200)
        self.assertEqual(Upstream.calls[-1], ("HEAD", "/api/news?q=oil", "127.0.0.1:8765"))
        self.assertEqual((len(AnalysisUpstream.calls), len(FuturexUpstream.calls)), other_calls)

    def test_new_roots_redirect_to_trailing_slashes_with_sanitized_query(self):
        calls = (len(Upstream.calls), len(AnalysisUpstream.calls), len(FuturexUpstream.calls))
        for root in ("/oil", "/futurex"):
            with self.subTest(root=root):
                status, headers, body = self.request(root + "?refresh=1&month=2026-10&week=2")
                self.assertEqual(status, 308)
                self.assertEqual(headers["Location"], root + "/?month=2026-10&week=2")
                self.assertEqual(body, b"")
        self.assertEqual((len(Upstream.calls), len(AnalysisUpstream.calls), len(FuturexUpstream.calls)), calls)

    def test_futurex_assets_use_their_own_backend_and_preserve_query(self):
        other_calls = (len(Upstream.calls), len(AnalysisUpstream.calls))
        for path, (content_type, expected) in FuturexUpstream.assets.items():
            with self.subTest(path=path):
                status, headers, body = self.request("/futurex" + path + "?month=2026-10&refresh=1")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], content_type)
                self.assertEqual(body, expected)
                self.assertEqual(FuturexUpstream.calls[-1], ("GET", path + "?month=2026-10", "127.0.0.1:8806"))
        self.assertEqual((len(Upstream.calls), len(AnalysisUpstream.calls)), other_calls)

    def test_futurex_head_keeps_content_length_and_returns_no_body(self):
        status, headers, body = self.request("/futurex/data.js", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(int(headers["Content-Length"]), len(FuturexUpstream.assets["/data.js"][1]))
        self.assertEqual(body, b"")
        self.assertEqual(FuturexUpstream.calls[-1], ("HEAD", "/data.js", "127.0.0.1:8806"))

    def test_new_namespaces_do_not_expand_the_existing_allowlists(self):
        calls = (len(Upstream.calls), len(AnalysisUpstream.calls), len(FuturexUpstream.calls))
        for path in (
            "/oil/../api/config", "/oil/%2e%2e/api/config", "/oil//app.js", "/oil/%61pp.js",
            "/oil/README.md", "/oil/analysis/exp01/", "/oil/futurex/", "/oil/api/settings/private",
            "/futurex/../app.js", "/futurex/%2e%2e/app.js", "/futurex//app.js", "/futurex/%61pp.js",
            "/futurex/README.md", "/futurex/category_catalog.json", "/futurex/api/config",
            "/futurex/analysis/exp01/", "/futurex/data.js/extra", "/futurex/.git/config",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)
        self.assertEqual((len(Upstream.calls), len(AnalysisUpstream.calls), len(FuturexUpstream.calls)), calls)

    def test_futurex_disconnect_is_identified_without_breaking_oil_or_analysis(self):
        with socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            with patch.object(self.proxy, "futurex_upstream_port", unavailable.getsockname()[1]):
                status, _, body = self.request("/futurex/")
                self.assertEqual(status, 502)
                self.assertIn("FutureX", body.decode())
                self.assertEqual(self.request("/oil/")[0], 200)
                self.assertEqual(self.request("/analysis/exp01/")[0], 200)

    def test_new_namespaces_reject_writes_before_any_backend(self):
        calls = (len(Upstream.calls), len(AnalysisUpstream.calls), len(FuturexUpstream.calls))
        for path in ("/oil/", "/oil/api/settings", "/futurex/", "/futurex/data.json"):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(path=path, method=method):
                    status, headers, _ = self.request(path, method=method)
                    self.assertEqual(status, 405)
                    self.assertEqual(headers["Allow"], "GET, HEAD")
        self.assertEqual((len(Upstream.calls), len(AnalysisUpstream.calls), len(FuturexUpstream.calls)), calls)


if __name__ == "__main__":
    unittest.main()
