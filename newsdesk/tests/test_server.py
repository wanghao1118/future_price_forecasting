"""Run with: python3 -m unittest discover -s newsdesk/tests -v"""

import importlib.util
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
from newsdesk.tests.publication_fixture import verified_metadata


MODULE_PATH = Path(__file__).resolve().parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("news_server", MODULE_PATH)
server_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server_module)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "news.sqlite3"
        self.store = server_module.NewsStore(self.database, seed_demo=False)

    def tearDown(self):
        self.temp.cleanup()

    def test_crud_and_persistence(self):
        item = self.store.save({"published_at": "2026-10-28T01:00:00+00:00", "title": "持久化测试", "category": "supply", "assets": ["WTI"], "channels": ["供应数量"]})
        reopened = server_module.NewsStore(self.database)
        self.assertEqual(reopened.get(item["id"])["title"], "持久化测试")
        updated = self.store.save({"summary": "保存后的补充信息"}, item["id"])
        self.assertEqual(updated["created_at"], item["created_at"])
        self.assertEqual(updated["summary"], "保存后的补充信息")
        self.assertTrue(self.store.delete(item["id"]))
        self.assertIsNone(reopened.get(item["id"]))

    def test_import_is_atomic_and_upserts(self):
        item = self.store.save({"published_at": "2026-10-28T01:00:00+00:00", "title": "原始记录", "category": "inventory"})
        replacement = dict(item, title="更新记录")
        with self.assertRaises(server_module.ValidationError):
            self.store.import_items([replacement, {"published_at": "2026-10-28T01:00:00+00:00", "title": "错误记录", "category": "unknown"}])
        self.assertEqual(self.store.get(item["id"])["title"], "原始记录")
        new = {"published_at": "2026-10-28T01:00:00+00:00", "title": "新记录", "category": "demand"}
        self.assertEqual(self.store.import_items([replacement, new]), 2)
        self.assertEqual(len(self.store.all()), 2)
        self.assertEqual(self.store.get(item["id"])["title"], "更新记录")

    def test_partial_import_preserves_fields_and_creation_timestamp(self):
        identifier = str(uuid.uuid4())
        created_at = "2020-01-01T00:00:00+00:00"
        updated_at = "2020-01-02T00:00:00+00:00"
        self.store.import_items([{"published_at": "2026-10-28T01:00:00+00:00", 
            "id": identifier, "title": "完整旧记录", "category": "supply",
            "summary": "不能丢失的摘要", "assets": ["WTI"], "channels": ["供应数量"],
            "source_name": "原始公告", "event_id": "event-retained",
            "created_at": created_at, "updated_at": updated_at,
        }])
        self.store.import_items([{"id": identifier.upper(), "title": "部分更新标题"}])
        updated = self.store.get(identifier)
        self.assertEqual(updated["title"], "部分更新标题")
        self.assertEqual(updated["summary"], "不能丢失的摘要")
        self.assertEqual(updated["assets"], ["WTI"])
        self.assertEqual(updated["source_name"], "原始公告")
        self.assertEqual(updated["event_id"], "event-retained")
        self.assertEqual(updated["created_at"], created_at)
        self.assertNotEqual(updated["updated_at"], updated_at)
        backup = dict(updated, created_at="2019-01-01T00:00:00+00:00", updated_at="2019-01-02T00:00:00+00:00")
        self.store.import_items([backup])
        restored = self.store.get(identifier)
        self.assertEqual(restored["created_at"], backup["created_at"])
        self.assertEqual(restored["updated_at"], backup["updated_at"])

    def test_duplicate_import_rejected_without_partial_update(self):
        original = self.store.save({"published_at": "2026-10-28T01:00:00+00:00", "title": "重复前原始标题", "category": "logistics"})
        with self.assertRaisesRegex(server_module.ValidationError, "重复"):
            self.store.import_items([
                {"id": original["id"], "title": "不应生效的第一条更新"},
                {"id": original["id"], "title": "重复更新"},
            ])
        self.assertEqual(self.store.get(original["id"])["title"], original["title"])

    def test_demo_initialized_once(self):
        other = Path(self.temp.name) / "demo.sqlite3"
        demo = server_module.NewsStore(other)
        self.assertEqual(len(demo.all()), 10)
        self.assertEqual({item["category"] for item in demo.all()}, {category["id"] for category in server_module.CATEGORIES})
        self.assertTrue(all(item["is_demo"] and "演示" in item["summary"] for item in demo.all()))
        self.assertEqual(demo.clear_demo(), 10)
        self.assertEqual(server_module.NewsStore(other).all(), [])

    def test_validation_rejects_invalid_fields(self):
        base = {"published_at": "2026-10-28T01:00:00+00:00", "title": "测试", "category": "weather"}
        invalid = [
            {"title": ""}, {"category": "unknown"}, {"assets": ["BTC"]},
            {"channels": "供应数量"}, {"source_url": "javascript:alert(1)"},
            {"source_url": "http://["}, {"published_at": "昨天"},
            {"verification": "随便"}, {"entities": [" "]}, {"is_demo": "true"},
        ]
        for changed in invalid:
            with self.subTest(changed=changed):
                with self.assertRaises(server_module.ValidationError):
                    self.store.save(dict(base, **changed))

    def test_categories_fallback_does_not_rewrite_legacy_record(self):
        item = self.store.save({"published_at": "2026-10-28T01:00:00+00:00", "title": "旧版单分类记录", "category": "supply"})
        legacy = dict(item)
        legacy.pop("categories")
        with self.store.connect() as connection:
            connection.execute("UPDATE news SET payload=? WHERE id=?", (json.dumps(legacy), item["id"]))
        self.assertEqual(self.store.get(item["id"])["categories"], ["supply"])
        self.assertEqual(self.store.all()[0]["categories"], ["supply"])
        with self.store.connect() as connection:
            payload = json.loads(connection.execute("SELECT payload FROM news WHERE id=?", (item["id"],)).fetchone()[0])
        self.assertNotIn("categories", payload)
        self.assertEqual(payload["updated_at"], legacy["updated_at"])

    def test_multiple_categories_and_legacy_primary_edit(self):
        item = self.store.save({"published_at": "2026-10-28T01:00:00+00:00", "title": "港口袭击影响交付", "category": "geopolitics", "categories": ["geopolitics", "logistics", "logistics"]})
        self.assertEqual(item["categories"], ["geopolitics", "logistics"])
        self.store.import_items([{"id": item["id"], "summary": "补充内容"}])
        self.assertEqual(self.store.get(item["id"])["categories"], ["geopolitics", "logistics"])
        updated = self.store.save({"category": "supply"}, item["id"])
        self.assertEqual(updated["categories"], ["supply", "geopolitics", "logistics"])
        for categories in [[], ["unknown"], [True], "geopolitics", ["logistics"], ["geopolitics"] * 11]:
            with self.subTest(categories=categories):
                with self.assertRaises(server_module.ValidationError):
                    self.store.save({"published_at": "2026-10-28T01:00:00+00:00", "title": "无效分类", "category": "geopolitics", "categories": categories})

    def reviewed_item(self):
        return {"published_at": "2026-10-28T01:00:00+00:00", 
            "title": "港口袭击影响原油交付", "category": "geopolitics", "categories": ["geopolitics", "logistics"],
            "agent_review": {
                "reviewer": "oil-news-agent", "reviewed_at": "2026-10-28T02:00:00+00:00", "relevance": True,
                "relevance_reason": "正文涉及原油港口与运输中断。",
                "category_reasons": {"geopolitics": "袭击事件。", "logistics": "影响原油交付。"},
                "evidence": ["原油港口受到袭击，部分油轮暂停装货。"], "confidence": "高",
            },
            "ingestion": {
                **verified_metadata(),
                "source_id": "public-feed", "external_id": "42", "canonical_url": "https://example.com/news/42",
                "raw_path": "ingestion/raw/run-1/source.json", "content_hash": "a" * 64, "run_id": "run-1",
            },
        }

    def test_agent_audit_and_ingestion_survive_partial_upsert(self):
        record = self.reviewed_item()
        identifier = str(uuid.uuid4())
        self.store.import_items([dict(record, id=identifier)])
        self.store.import_items([{"id": identifier, "summary": "补充摘要", "ingestion": {"run_id": "run-2"}}])
        saved = self.store.get(identifier)
        self.assertEqual(saved["agent_review"], record["agent_review"])
        self.assertEqual(saved["ingestion"]["run_id"], "run-2")
        self.assertEqual(saved["ingestion"]["content_hash"], record["ingestion"]["content_hash"])
        self.assertEqual(saved["categories"], record["categories"])

    def test_manual_category_edit_keeps_original_agent_audit(self):
        record = self.reviewed_item()
        original = self.store.save(record)
        updated = self.store.save({"category": "supply", "categories": ["supply"]}, original["id"])
        self.assertEqual(updated["categories"], ["supply"])
        self.assertEqual(updated["agent_review"], record["agent_review"])
        # Subsequent partial updates must also retain the audit rather than
        # demand a fabricated review of the person's new classification.
        self.store.import_items([{"id": original["id"], "summary": "人工补充摘要"}])
        self.assertEqual(self.store.get(original["id"])["agent_review"], record["agent_review"])
        with self.assertRaises(server_module.ValidationError):
            self.store.save({"agent_review": record["agent_review"]}, original["id"])

    def test_agent_audit_requires_evidence_for_every_classification(self):
        record = self.reviewed_item()
        invalid = [
            None, [], {},
            dict(record["agent_review"], relevance="true"),
            dict(record["agent_review"], reviewed_at="2026-10-28T10:00:00"),
            dict(record["agent_review"], category_reasons={"geopolitics": "袭击事件。"}),
            dict(record["agent_review"], category_reasons={"unknown": "未知"}),
            dict(record["agent_review"], evidence=[]),
            dict(record["agent_review"], evidence=[False]),
            dict(record["agent_review"], confidence="high"),
        ]
        for review in invalid:
            with self.subTest(review=review):
                with self.assertRaises(server_module.ValidationError):
                    self.store.save(dict(record, agent_review=review))
        self.assertEqual(self.store.all(), [])

    def test_ingestion_rejects_invalid_provenance(self):
        for ingestion in [None, [], {"source_id": 42}, {"raw_path": "x" * 4097}, {"canonical_url": "file:///tmp/raw"}, {"canonical_url": "https://["}, {"token": "should-not-be-stored"}]:
            with self.subTest(ingestion=ingestion):
                with self.assertRaises(server_module.ValidationError):
                    self.store.save({"published_at": "2026-10-28T01:00:00+00:00", "title": "错误抓取元数据", "category": "supply", "ingestion": ingestion})


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = server_module.create_server(port=0, database=Path(cls.temp.name) / "api.sqlite3", seed_demo=False)
        cls.port = cls.server.server_port
        cls.base = "http://127.0.0.1:{}/".format(cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def request(self, path, method="GET", payload=None, headers=None):
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(self.base + path.lstrip("/"), data=body, method=method, headers=request_headers)
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_review_exclusions_are_hidden_but_preserved_for_audit(self):
        payload = {"published_at": "2026-10-28T01:00:00+00:00", "title": "复核排除测试", "category": "positioning", "agent_review": {
            "reviewer": "Codex CLI · gpt-6.1-sol", "reviewed_at": "2026-10-28T03:00:00+00:00",
            "relevance": False, "relevance_reason": "纯行情报价，没有资金或交易制度因子。",
            "category_reasons": {}, "evidence": [], "confidence": "高"}}
        status, item = self.request('/api/news', 'POST', payload)
        self.assertEqual(status, 201)
        _, visible = self.request('/api/news')
        self.assertNotIn(item['id'], [row['id'] for row in visible['items']])
        _, full = self.request('/api/news?include_excluded=1')
        self.assertIn(item['id'], [row['id'] for row in full['items']])
        _, preserved = self.request('/api/news/' + item['id'])
        self.assertEqual(preserved['title'], payload['title'])
        self.request('/api/news/' + item['id'], 'DELETE', {})

    def test_api_crud_and_export(self):
        status, item = self.request("/api/news", "POST", {"published_at": "2026-10-28T01:00:00+00:00", "title": "API 新建", "category": "macro"})
        self.assertEqual(status, 201)
        status, loaded = self.request("/api/news/" + item["id"])
        self.assertEqual(loaded["title"], item["title"])
        status, updated = self.request("/api/news/" + item["id"], "PUT", {"title": "API 更新"})
        self.assertEqual(status, 200)
        status, exported = self.request("/api/export")
        self.assertTrue(any(record["id"] == item["id"] for record in exported["items"]))
        self.assertEqual(exported["version"], 1)
        status, result = self.request("/api/news/" + item["id"], "DELETE")
        self.assertEqual((status, result["ok"]), (200, True))
        status, _ = self.request("/api/news/" + item["id"])
        self.assertEqual(status, 404)

    def test_csrf_and_host_protection(self):
        payload = {"published_at": "2026-10-28T01:00:00+00:00", "title": "跨站写入", "category": "supply"}
        for headers, expected in [
            ({"Origin": "https://outside.example"}, 403),
            ({"Content-Type": "text/plain"}, 415),
            ({"Host": "outside.example:{}".format(self.port)}, 403),
        ]:
            with self.subTest(headers=headers):
                status, _ = self.request("/api/news", "POST", payload, headers)
                self.assertEqual(status, expected)
        status, item = self.request("/api/news", "POST", payload, {"Origin": "http://localhost:{}".format(self.port)})
        self.assertEqual(status, 201)
        self.request("/api/news/" + item["id"], "DELETE")

    def test_config_filter_and_bad_import(self):
        status, config = self.request("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(len(config["categories"]), 10)
        status, created = self.request("/api/news", "POST", {"published_at": "2026-10-28T01:00:00+00:00", "title": "特殊检索文字", "category": "logistics", "assets": ["SC"]})
        status, result = self.request("/api/news?category=logistics&asset=SC")
        self.assertIn(created["id"], [item["id"] for item in result["items"]])
        status, _ = self.request("/api/import", "POST", {"items": [{"published_at": "2026-10-28T01:00:00+00:00", "title": "不会部分写入", "category": "supply"}, {"title": "缺分类"}]})
        self.assertEqual(status, 400)
        self.assertFalse(any(item["title"] == "不会部分写入" for item in self.server.store.all()))
        self.request("/api/news/" + created["id"], "DELETE")

    def test_secondary_category_is_searchable_without_duplicate_record(self):
        status, created = self.request("/api/news", "POST", {"published_at": "2026-10-28T01:00:00+00:00", 
            "title": "多分类运输报道", "category": "geopolitics", "categories": ["geopolitics", "logistics"], "assets": ["Brent"],
        })
        self.assertEqual(status, 201)
        for category in ["geopolitics", "logistics"]:
            status, result = self.request("/api/news?category={}&asset=Brent".format(category))
            self.assertEqual(status, 200)
            self.assertEqual([row["id"] for row in result["items"]].count(created["id"]), 1)
        status, result = self.request("/api/news?category=weather&asset=Brent")
        self.assertNotIn(created["id"], [row["id"] for row in result["items"]])
        self.request("/api/news/" + created["id"], "DELETE")

    def test_static_cannot_expose_database(self):
        for path in ["/data/news.sqlite3", "/../server.py", "/%2e%2e/server.py", "/api/not-found"]:
            with self.subTest(path=path):
                status, _ = self.request(path)
                self.assertEqual(status, 404)

    def test_contract_indicators_route_and_refresh_are_independent_of_chart_period(self):
        payload = {"contract": {"id": "sc-202612"}, "as_of": "2026-10-29", "rows": [
            {"id": "open_interest", "label": "持仓量", "value": "27,626 手", "status": "available"},
        ]}
        with patch.object(self.server.market_indicators, "get", return_value=payload) as indicators, \
                patch.object(self.server.market_data, "get") as candles:
            self.assertEqual(self.request("/api/market/sc-202612/indicators?period=1h"), (200, payload))
            indicators.assert_called_once_with("sc-202612", refresh=False)
            candles.assert_not_called()
            self.assertEqual(self.request("/api/market/sc-202612/indicators?refresh=1")[0], 200)
            self.assertEqual(indicators.call_args.kwargs, {"refresh": True})
        self.assertEqual(self.request("/api/market/unknown/indicators")[0], 404)
        self.assertEqual(self.request("/api/market/sc-202612/extra/indicators")[0], 404)

    def test_indicator_history_range_is_bounded_before_fetching(self):
        payload = {"contract": {"id": "sc-202612"}, "rows": [], "history": True, "days": 30}
        with patch.object(self.server.market_indicators, "get", return_value=payload) as indicators:
            self.assertEqual(self.request("/api/market/sc-202612/indicators?history=1"), (200, payload))
            indicators.assert_called_once_with("sc-202612", refresh=False, history=True, days=30)
            indicators.reset_mock()
            for query in ["history=yes", "history=1&days=0", "history=1&days=31", "history=1&days=1.5", "history=1&days=x"]:
                with self.subTest(query=query):
                    self.assertEqual(self.request("/api/market/sc-202612/indicators?" + query)[0], 400)
            indicators.assert_not_called()
            self.assertEqual(self.request("/api/market/sc-202612/indicators?history=1&days=10&refresh=1")[0], 200)
            indicators.assert_called_once_with("sc-202612", refresh=True, history=True, days=10)

    def test_indicator_history_dates_match_the_chart_view_and_are_validated(self):
        payload = {"contract": {"id": "sc-202612"}, "rows": []}
        with patch.object(self.server.market_indicators, "get", return_value=payload) as indicators:
            self.assertEqual(self.request("/api/market/sc-202612/indicators?history=1&start=2026-07-01&end=2026-10-30"), (200, payload))
            indicators.assert_called_once_with("sc-202612", refresh=False, history=True, start="2026-07-01", end="2026-10-30")
            indicators.reset_mock()
            for query in ["start=2026-07-01", "end=2026-10-30", "start=2026-7-1&end=2026-10-30",
                          "start=2026-02-30&end=2026-10-30", "start=2026-10-30&end=2026-07-01"]:
                with self.subTest(query=query):
                    self.assertEqual(self.request("/api/market/sc-202612/indicators?history=1&" + query)[0], 400)
            indicators.assert_not_called()

    def test_update_settings_are_validated_and_persisted(self):
        status, original = self.request("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(original["news_interval_minutes"], 30)
        self.assertEqual(original["contract_interval_minutes"], 1440)
        self.assertEqual(set(original["jobs"]), {"news", "contracts"})
        try:
            status, updated = self.request("/api/settings", "PUT", {"news_interval_minutes": 45})
            self.assertEqual(status, 200)
            self.assertEqual(updated["news_interval_minutes"], 45)
            self.assertEqual(json.loads((self.server.store.path.parent / "settings.json").read_text())["news_interval_minutes"], 45)
            for invalid in [{"news_interval_minutes": 0}, {"contract_interval_minutes": True}, {"jobs": {}}, {"news_interval_minutes": 0.1}]:
                status, _ = self.request("/api/settings", "PUT", invalid)
                self.assertEqual(status, 400)
            status, unchanged = self.request("/api/settings")
            self.assertEqual(unchanged["news_interval_minutes"], 45)
            status, _ = self.request("/api/settings", "PUT", {"news_interval_minutes": 60}, {"Origin": "https://outside.example"})
            self.assertEqual(status, 403)
        finally:
            self.request("/api/settings", "PUT", {"news_interval_minutes": 30, "contract_interval_minutes": 1440})

    def test_manual_update_is_async_and_duplicate_request_is_rejected(self):
        started, release = threading.Event(), threading.Event()
        def offline_runner():
            started.set()
            release.wait(timeout=3)
            return {"status": "success", "written": 0}
        with patch.dict(self.server.updates.runners, {"news": offline_runner}):
            try:
                status, running = self.request("/api/updates/news", "POST", {})
                self.assertEqual(status, 202)
                self.assertTrue(started.wait(timeout=1))
                self.assertTrue(running["jobs"]["news"]["running"])
                status, _ = self.request("/api/updates/news", "POST", {})
                self.assertEqual(status, 409)
                # Ordinary HTTP reads continue while the update is blocked.
                status, _ = self.request("/api/news")
                self.assertEqual(status, 200)
                status, _ = self.request("/api/updates/contracts", "POST", {"unknown": True})
                self.assertEqual(status, 400)
            finally:
                release.set()
                self.server.updates._workers["news"].join(timeout=3)

    def test_head_returns_get_headers_without_body(self):
        for path in ["/", "/api/config"]:
            with self.subTest(path=path):
                with urlopen(Request(self.base + path.lstrip("/"), method="GET"), timeout=3) as response:
                    expected_length = len(response.read())
                    expected_content_type = response.headers["Content-Type"]
                with urlopen(Request(self.base + path.lstrip("/"), method="HEAD"), timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"")
                    self.assertEqual(int(response.headers["Content-Length"]), expected_length)
                    self.assertEqual(response.headers["Content-Type"], expected_content_type)

    def test_source_maintenance_lists_enabled_sources_and_validates_probe_requests(self):
        status, value = self.request('/api/sources/health')
        self.assertEqual(status,200)
        self.assertEqual(value['total'],81)
        self.assertEqual(sum(bool(s['authority_row']) for s in value['sources']),39)
        for payload in [{'source_ids':'jin10'},{'source_ids':['unknown']},{'url':'http://internal/'}]:
            status,_ = self.request('/api/sources/check','POST',payload)
            self.assertEqual(status,400)
        status,_ = self.request('/api/sources/check','POST',{}, {'Origin':'https://outside.example'})
        self.assertEqual(status,403)
        with patch.object(self.server.sources,'trigger',return_value=True) as trigger:
            status,_ = self.request('/api/sources/check','POST',{'source_ids':['authority_26']})
            self.assertEqual(status,202)
            trigger.assert_called_once_with(['authority_26'])
        with patch.object(self.server.sources,'trigger',return_value=False):
            status,_ = self.request('/api/sources/check','POST',{})
            self.assertEqual(status,409)


if __name__ == "__main__":
    unittest.main()
