#!/usr/bin/env python3
"""Local gold futures workbench with an isolated UTC news database.

The gold workbench deliberately exposes only contract charts and the news
archive. Experiment setup, benchmark/evaluation pages, and strategy records
remain in the oil workbench and are not copied here.
"""
import argparse
import json
import mimetypes
import signal
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
DEFAULT_DATABASE = ROOT / "data" / "gold.sqlite3"
MAX_REQUEST_BYTES = 8 * 1024 * 1024

if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT.parent))

# Reuse the oil workbench's strict UTC-aware field validation and SQLite record
# format, but replace its taxonomy and asset vocabulary in this process.
import newsdesk.server as base

CATEGORIES = [
    {"id": "supply", "code": "01", "name": "黄金生产与矿产供应", "description": "金矿产量、矿山扰动、冶炼与再生金供应"},
    {"id": "demand", "code": "02", "name": "实物消费与工业需求", "description": "珠宝、投资金条、电子与工业用金需求"},
    {"id": "inventory", "code": "03", "name": "库存、仓单与现货", "description": "交易所库存、仓单、现货溢价和可交割资源"},
    {"id": "refining", "code": "04", "name": "精炼与回收", "description": "精炼产能、回收金、冶炼加工与供应链"},
    {"id": "geopolitics", "code": "05", "name": "地缘政治与避险", "description": "冲突、制裁、制裁规避和避险需求变化"},
    {"id": "logistics", "code": "06", "name": "贸易、运输与交付", "description": "跨境贸易、港口、运输、交割与地区价差"},
    {"id": "macro", "code": "07", "name": "宏观金融与流动性", "description": "央行、利率、美元、通胀与流动性"},
    {"id": "policy", "code": "08", "name": "央行与官方储备", "description": "央行购金、官方储备、货币政策与监管"},
    {"id": "positioning", "code": "09", "name": "期货资金与交易制度", "description": "基金持仓、ETF流量、保证金、限仓与交割制度"},
    {"id": "weather", "code": "10", "name": "天气、灾害与矿区风险", "description": "极端天气、灾害、矿区停产与电力供给风险"},
]
CHANNELS = ["矿产供应", "实物需求", "库存仓单", "精炼回收", "运输交付", "避险溢价", "汇率利率", "资金交易"]
ASSETS = ["AU"]
base.CATEGORIES = CATEGORIES
base.CHANNELS = CHANNELS
base.ASSETS = ASSETS

from golddesk.market_data import MarketData, MarketDataError
from newsdesk.market_indicators import MarketIndicators


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GoldHandler(BaseHTTPRequestHandler):
    server_version = "GoldSignal/1.0"

    def json_response(self, data, status=200, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def valid_host(self):
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit("//" + host)
            return parsed.hostname in {"localhost", "127.0.0.1", "::1"} and (parsed.port or 80) == self.server.server_port
        except ValueError:
            return False

    def check_request(self, write=False):
        if not self.valid_host():
            self.json_response({"error": "只接受本机 localhost 请求"}, 403)
            return False
        if write:
            content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                self.json_response({"error": "写入请求必须使用 application/json"}, 415)
                return False
            origin = self.headers.get("Origin")
            if origin:
                try:
                    parsed = urlsplit(origin)
                    valid = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"} and (parsed.port or 80) == self.server.server_port
                except ValueError:
                    valid = False
                if not valid:
                    self.json_response({"error": "不允许跨站写入本地档案"}, 403)
                    return False
        return True

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise base.ValidationError("Content-Length 无效")
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise base.ValidationError("请求内容过大，最大允许 8 MB")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, UnicodeDecodeError):
            raise base.ValidationError("请求内容不是有效的 JSON")
        if not isinstance(value, dict):
            raise base.ValidationError("请求必须是 JSON 对象")
        return value

    def do_GET(self):
        if not self.check_request():
            return
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path == "/api/config":
                self.json_response({"categories": CATEGORIES, "channels": CHANNELS, "assets": ASSETS, **base.ENUMS,
                                    "storage": "SQLite · golddesk 独立数据库", "timezone": "UTC+0", "version": 1})
            elif path == "/api/settings":
                self.json_response(self.server.settings_snapshot())
            elif path == "/api/sources/health":
                self.json_response({"checked_at": now_iso(), "sources": [], "summary": {"total": 0, "available": 0, "issues": 0}})
            elif path == "/api/market/contracts":
                self.json_response(self.server.market_data.catalog())
            elif path.startswith("/api/market/") and path.endswith("/indicators"):
                identifier = unquote(path[len("/api/market/"):-len("/indicators")])
                options = {"refresh": query.get("refresh", ["0"])[0] == "1"}
                history = query.get("history", ["0"])[0]
                if history not in {"0", "1"}:
                    self.json_response({"error": "history 必须是 0 或 1"}, 400)
                    return
                if history == "1":
                    start, end = query.get("start", [None])[0], query.get("end", [None])[0]
                    if start is not None or end is not None:
                        try:
                            if not start or not end:
                                raise ValueError
                            for value in (start, end):
                                if datetime.strptime(value, "%Y-%m-%d").date().isoformat() != value:
                                    raise ValueError
                            if start > end:
                                raise ValueError
                        except ValueError:
                            self.json_response({"error": "start 和 end 必须是有效且顺序正确的 YYYY-MM-DD 日期"}, 400)
                            return
                        options.update(history=True, start=start, end=end)
                    else:
                        try:
                            days = int(query.get("days", ["30"])[0])
                            if not 1 <= days <= 30:
                                raise ValueError
                        except ValueError:
                            self.json_response({"error": "历史指标范围必须是 1–30 个交易日"}, 400)
                            return
                        options.update(history=True, days=days)
                self.json_response(self.server.market_indicators.get(identifier, **options))
            elif path.startswith("/api/market/"):
                identifier = unquote(path[len("/api/market/"):])
                self.json_response(self.server.market_data.get(identifier,
                    period=query.get("period", ["1d"])[0], refresh=query.get("refresh", ["0"])[0] == "1"))
            elif path == "/api/news":
                items = self.server.store.all()
                if query.get("include_excluded", ["0"])[0] != "1":
                    items = [item for item in items if item.get("agent_review", {}).get("relevance") is not False]
                if query.get("category"):
                    items = [item for item in items if query["category"][0] in item.get("categories", [])]
                for field in ["direction", "verification", "source_type"]:
                    if query.get(field):
                        items = [item for item in items if item.get(field) == query[field][0]]
                if query.get("asset"):
                    items = [item for item in items if query["asset"][0] in item.get("assets", [])]
                if query.get("q"):
                    needle = query["q"][0].casefold()
                    items = [item for item in items if needle in json.dumps(item, ensure_ascii=False).casefold()]
                self.json_response({"items": items, "total": len(items), "timezone": "UTC+0"})
            elif path == "/api/export":
                self.json_response({"version": 1, "exported_at": now_iso(), "timezone": "UTC+0", "items": self.server.store.all()},
                                    headers={"Content-Disposition": 'attachment; filename="gold-signal-news.json"'})
            elif path.startswith("/api/news/"):
                item = self.server.store.get(unquote(path[len("/api/news/"):]))
                self.json_response(item if item else {"error": "新闻不存在"}, 200 if item else 404)
            elif path.startswith("/api/"):
                self.json_response({"error": "接口不存在"}, 404)
            else:
                self.serve_static(parsed.path)
        except (MarketDataError, base.ValidationError) as error:
            self.json_response({"error": getattr(error, "message", str(error))}, getattr(error, "status", 400))
        except (KeyError, ValueError, TypeError) as error:
            self.json_response({"error": str(error) or "请求参数无效"}, 400)
        except OSError as error:
            self.json_response({"error": "本地资源暂时不可用：{}".format(type(error).__name__)}, 502)

    def do_HEAD(self):
        self.do_GET()

    def serve_static(self, request_path):
        decoded = unquote(request_path)
        root = STATIC_ROOT.resolve()
        target = (root / ("index.html" if decoded == "/" else decoded.lstrip("/"))).resolve()
        if root not in target.parents or not target.is_file():
            self.json_response({"error": "文件不存在"}, 404)
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_POST(self):
        self.mutate("POST")

    def do_PUT(self):
        self.mutate("PUT")

    def do_DELETE(self):
        self.mutate("DELETE")

    def mutate(self, method):
        if not self.check_request(write=True):
            return
        path = urlsplit(self.path).path.rstrip("/")
        try:
            data = self.read_json()
            if method == "POST" and path == "/api/news":
                self.json_response(self.server.store.save(data), 201)
            elif method == "POST" and path == "/api/import":
                self.json_response({"imported": self.server.store.import_items(data.get("items"))})
            elif method == "POST" and path == "/api/demo/clear":
                self.json_response({"ok": True, "deleted": self.server.store.clear_demo()})
            elif method == "PUT" and path == "/api/settings":
                self.json_response(self.server.update_settings(data))
            elif path.startswith("/api/news/") and method in {"PUT", "DELETE"}:
                identifier = unquote(path[len("/api/news/"):])
                if method == "PUT":
                    self.json_response(self.server.store.save(data, identifier))
                elif self.server.store.delete(identifier):
                    self.json_response({"ok": True})
                else:
                    self.json_response({"error": "新闻不存在"}, 404)
            else:
                self.json_response({"error": "接口不存在或请求方法不支持"}, 404)
        except base.ValidationError as error:
            self.json_response({"error": str(error)}, 400)
        except KeyError:
            self.json_response({"error": "新闻不存在"}, 404)
        except sqlite3.Error:
            self.json_response({"error": "本地数据库写入失败，请稍后重试"}, 500)
        except (OSError, ValueError, TypeError) as error:
            self.json_response({"error": str(error) or "请求参数无效"}, 400)


class GoldServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, database, seed_demo=False):
        self.store = base.NewsStore(database, seed_demo=seed_demo)
        market_cache = Path(database).parent / "market"
        self.market_data = MarketData(cache_dir=market_cache)
        self.market_indicators = MarketIndicators(self.market_data)
        self._settings_path = Path(database).parent / "settings.json"
        self._settings = self._load_settings()
        super().__init__(address, GoldHandler)

    def _load_settings(self):
        default = {"news_interval_minutes": 30, "contract_interval_minutes": 1440,
                   "jobs": {"news": {"status": "idle", "running": False},
                            "contracts": {"status": "idle", "running": False}}}
        try:
            value = json.loads(self._settings_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                default.update({key: value[key] for key in ("news_interval_minutes", "contract_interval_minutes") if key in value})
        except (OSError, ValueError, TypeError):
            pass
        return default

    def settings_snapshot(self):
        return json.loads(json.dumps(self._settings, ensure_ascii=False))

    def update_settings(self, data):
        allowed = {"news_interval_minutes", "contract_interval_minutes"}
        if set(data) - allowed:
            raise base.ValidationError("设置仅支持新闻和黄金行情更新间隔")
        for key in allowed:
            if key in data and (type(data[key]) is not int or not 1 <= data[key] <= 10080):
                raise base.ValidationError("更新间隔须为 1 分钟至 7 天之间的整数分钟")
        self._settings.update({key: data[key] for key in allowed if key in data})
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._settings_path.write_text(json.dumps(self._settings, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.settings_snapshot()

    def server_close(self):
        self.market_indicators.close()
        super().server_close()


def create_server(host="127.0.0.1", port=8785, database=DEFAULT_DATABASE, seed_demo=False):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("服务只允许绑定 127.0.0.1 或 localhost")
    return GoldServer((host, port), database, seed_demo=seed_demo)


def main():
    parser = argparse.ArgumentParser(description="黄金期货新闻本地工作台（UTC+0）")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8785)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--seed-demo", action="store_true", help="仅用于界面检查，写入演示记录")
    args = parser.parse_args()
    try:
        server = create_server(args.host, args.port, args.database, seed_demo=args.seed_demo)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, "无法启动黄金工作台：{}\n".format(error))
    signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt()))
    print("GOLD / SIGNAL · 黄金期货新闻研究台 · UTC+0", flush=True)
    print("Local: http://localhost:{}/".format(server.server_port), flush=True)
    print("Database: {}".format(args.database.resolve()), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n黄金工作台已停止。", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
