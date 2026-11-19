#!/usr/bin/env python3
"""Local oil-news archive. Python 3.9+; no third-party dependencies."""

import argparse
import json
import mimetypes
import re
import signal
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

# Keep the documented ``python3 newsdesk/server.py`` entry point compatible
# with packages shared by the research and evaluation modules.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from newsdesk.private_config import load_private_aihub_config
from newsdesk.source_authority import assess as assess_authority, enrich as enrich_authority

try:
    from newsdesk.market_data import MarketData, MarketDataError
    from newsdesk.market_indicators import MarketIndicators
    from newsdesk.market_supplemental import SupplementalData
    from newsdesk.scheduler import UpdateScheduler, run_news_update
except ModuleNotFoundError:
    from market_data import MarketData, MarketDataError
    from market_indicators import MarketIndicators
    from market_supplemental import SupplementalData
    from scheduler import UpdateScheduler, run_news_update


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
DEFAULT_DATABASE = ROOT / "data" / "news.sqlite3"
MAX_REQUEST_BYTES = 8 * 1024 * 1024

CATEGORIES = [
    {"id": "supply", "code": "01", "name": "原油生产与供应", "description": "产量、OPEC+ 决策、油田运行与上游投资"},
    {"id": "demand", "code": "02", "name": "消费需求与经济活动", "description": "工业、交通、航空、采购与原油需求预期"},
    {"id": "inventory", "code": "03", "name": "库存与战略储备", "description": "商业库存、库欣库存、浮仓与战略储备"},
    {"id": "refining", "code": "04", "name": "炼厂与成品油", "description": "炼厂开工、检修、加工利润与成品油供需"},
    {"id": "geopolitics", "code": "05", "name": "地缘政治与国际制裁", "description": "冲突、谈判、产油国政局与制裁执行"},
    {"id": "logistics", "code": "06", "name": "运输、贸易与交付", "description": "海峡、港口、管道、油轮、运费与交付"},
    {"id": "macro", "code": "07", "name": "宏观金融与流动性", "description": "央行、利率、美元、信用与市场流动性"},
    {"id": "policy", "code": "08", "name": "能源政策与替代能源", "description": "税收、进出口配额、环保与燃料替代"},
    {"id": "positioning", "code": "09", "name": "期货资金与交易制度", "description": "基金持仓、套保、保证金、限仓与交割"},
    {"id": "weather", "code": "10", "name": "天气与自然灾害", "description": "飓风、严寒、洪水、高温及生产运输影响"},
]
CHANNELS = ["供应数量", "消费需求", "库存", "炼厂加工", "运输交付", "风险溢价", "汇率利率", "资金交易"]
ENUMS = {
    "source_type": ["事实公告", "数据发布", "机构预测", "评论复盘", "未经确认消息"],
    "stage": ["传闻", "提议", "宣布", "实施", "扩大", "恢复", "取消"],
    "horizon": ["盘中", "1–5个交易日", "数周", "数月及以上"],
    "direction": ["偏多", "偏空", "混合", "不确定"],
    "verification": ["待核验", "已确认", "存在争议"],
    "confidence": ["低", "中", "高"],
}
ASSETS = ["WTI", "Brent", "SC"]
TEXT_FIELDS = [
    "title", "summary", "body", "category", "contract", "region", "source_name",
    "source_url", "source_type", "origin_name", "origin_evidence", "origin_method", "published_at", "occurred_at", "collected_at",
    "origin_status", "origin_reason", "origin_reviewed_at", "origin_review_version", "origin_content_hash",
    "effective_at", "stage", "actual_value", "expected_value", "previous_value",
    "unit", "scale", "duration", "horizon", "direction", "rationale",
    "verification", "confidence", "event_id", "update_note",
]
DATE_FIELDS = ["published_at", "occurred_at", "collected_at", "effective_at"]
INGESTION_FIELDS = ("source_id", "external_id", "canonical_url", "raw_path", "content_hash", "run_id",
                    "collection_platform", "publisher_name", "content_scope", "published_date", "publication_precision",
                    "public_source_url", "detail_access", "source_page_kind",
                    "source_published_at", "source_timezone", "timezone_status", "timezone_evidence",
                    "timezone_checked_at", "timezone_audit_version", "original_published_at",
                    "publication_raw_path", "publication_raw_sha256")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ValidationError(ValueError):
    pass


def valid_date(value, field):
    if not value:
        return
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise ValidationError("{} 必须是有效的 ISO 日期时间".format(field))


def validate_agent_review(value, categories):
    if not isinstance(value, dict):
        raise ValidationError("agent_review 必须是对象")
    result = {}
    for field, limit in [("reviewer", 200), ("reviewed_at", 100), ("relevance_reason", 4000)]:
        text = value.get(field)
        if not isinstance(text, str) or not text.strip() or len(text) > limit:
            raise ValidationError("agent_review.{} 必须是非空文本且不超过 {} 字符".format(field, limit))
        result[field] = text.strip()
    valid_date(result["reviewed_at"], "agent_review.reviewed_at")
    reviewed = datetime.fromisoformat(result["reviewed_at"].replace("Z", "+00:00"))
    if reviewed.tzinfo is None:
        raise ValidationError("agent_review.reviewed_at 必须包含时区")
    relevant = value.get("relevance")
    if not isinstance(relevant, bool):
        raise ValidationError("agent_review.relevance 必须是布尔值")
    result["relevance"] = relevant
    reasons = value.get("category_reasons")
    if not isinstance(reasons, dict) or len(reasons) > len(CATEGORIES):
        raise ValidationError("agent_review.category_reasons 必须是分类与依据对象")
    if any(key not in categories or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000 for key, reason in reasons.items()):
        raise ValidationError("agent_review.category_reasons 仅能包含本条新闻的分类及非空依据")
    if relevant and set(reasons) != set(categories):
        raise ValidationError("agent_review.category_reasons 必须为每个分类提供依据")
    result["category_reasons"] = {key: reason.strip() for key, reason in reasons.items()}
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > 20 or (relevant and not evidence):
        raise ValidationError("agent_review.evidence 必须是最多 20 条证据的数组，相关新闻至少包含一条证据")
    if any(not isinstance(entry, str) or not entry.strip() or len(entry) > 4000 for entry in evidence):
        raise ValidationError("agent_review.evidence 中的每条证据必须是非空文本且不超过 4000 字符")
    result["evidence"] = list(dict.fromkeys(entry.strip() for entry in evidence))
    if value.get("confidence") not in ENUMS["confidence"]:
        raise ValidationError("agent_review.confidence 必须为低、中或高")
    result["confidence"] = value["confidence"]
    return result


def validate_ingestion(value):
    if not isinstance(value, dict):
        raise ValidationError("ingestion 必须是对象")
    if any(key not in INGESTION_FIELDS for key in value):
        raise ValidationError("ingestion 包含未知字段")
    result = {}
    for field, text in value.items():
        if not isinstance(text, str) or len(text) > 4096:
            raise ValidationError("ingestion.{} 必须是最多 4096 字符的文本".format(field))
        result[field] = text.strip()
    if result.get("canonical_url"):
        try:
            parsed = urlsplit(result["canonical_url"])
        except ValueError:
            raise ValidationError("ingestion.canonical_url 格式无效")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValidationError("ingestion.canonical_url 必须是 http 或 https 网址")
    if result.get("public_source_url"):
        try:
            public_url = urlsplit(result["public_source_url"])
        except ValueError:
            raise ValidationError("公开摘要链接格式无效")
        if public_url.scheme not in {"http", "https"} or not public_url.netloc:
            raise ValidationError("公开摘要链接必须是 http 或 https 网址")
    return result


def category_fallback(item):
    """Expose legacy single-category records without rewriting their history."""
    if "categories" not in item:
        item = dict(item, categories=[item["category"]])
    return item


def validate_news(data, existing=None, preserve_timestamps=False):
    if not isinstance(data, dict):
        raise ValidationError("新闻必须是 JSON 对象")
    merged = dict(existing or {})
    merged.update(data)
    item = {}
    for field in TEXT_FIELDS:
        value = merged.get(field, "")
        if value is None:
            value = ""
        if field in {"actual_value", "expected_value", "previous_value", "scale"} and isinstance(value, (float, int)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise ValidationError("{} 必须是文本".format(field))
        limit = 200000 if field == "body" else (400 if field == "title" else 20000)
        if len(value) > limit:
            raise ValidationError("{} 内容过长".format(field))
        item[field] = value.strip()
    if not item["title"]:
        raise ValidationError("请填写新闻标题")
    if len(item['origin_name']) > 200 or len(item['origin_evidence']) > 1000 or len(item['origin_reason']) > 1000:
        raise ValidationError('来源机构最多 200 字，溯源依据最多 1000 字')
    if item['origin_status'] not in {'','identified','unresolved','multiple_sources','not_applicable'}:
        raise ValidationError('溯源状态无效')
    if existing and 'origin_name' in data and data['origin_name'] != existing.get('origin_name', '') and 'origin_method' not in data:
        item['origin_method'] = 'manual'
        item['origin_evidence'] = ''
        item['origin_status'] = 'identified' if item['origin_name'] else 'unresolved'
        item['origin_reason'] = '人工核对并修正来源机构' if item['origin_name'] else '人工核对后清空来源机构，尚无法确认'
        item['origin_reviewed_at'] = now_iso()
        item['origin_review_version'] = 'manual'
        item['origin_content_hash'] = ''
    category_ids = {category["id"] for category in CATEGORIES}
    if item["category"] not in category_ids:
        raise ValidationError("请选择有效的新闻分类")
    categories = merged.get("categories", [item["category"]])
    if not isinstance(categories, list) or not categories or len(categories) > len(CATEGORIES):
        raise ValidationError("categories 必须是 1 至 10 项分类 ID 的数组")
    if any(not isinstance(entry, str) or entry not in category_ids for entry in categories):
        raise ValidationError("categories 包含无效的新闻分类")
    categories = list(dict.fromkeys(categories))
    if item["category"] not in categories:
        if "categories" in data:
            raise ValidationError("主分类 category 必须包含在 categories 中")
        # Legacy editors submit category alone. Keep the existing secondary
        # classifications instead of silently deleting them on such updates.
        categories.insert(0, item["category"])
    item["categories"] = categories
    defaults = {
        "source_type": "事实公告", "stage": "宣布", "horizon": "1–5个交易日",
        "direction": "不确定", "verification": "待核验", "confidence": "中",
    }
    for field, choices in ENUMS.items():
        item[field] = item[field] or defaults[field]
        if item[field] not in choices:
            raise ValidationError("{} 的选项无效".format(field))
    for field, choices in [("channels", CHANNELS), ("assets", ASSETS), ("entities", None)]:
        value = merged.get(field, [])
        if not isinstance(value, list) or len(value) > 50:
            raise ValidationError("{} 必须是最多 50 项的数组".format(field))
        if any(not isinstance(entry, str) or not entry.strip() or len(entry) > 200 for entry in value):
            raise ValidationError("{} 中的每一项必须是非空文本".format(field))
        value = list(dict.fromkeys(entry.strip() for entry in value))
        if choices and any(entry not in choices for entry in value):
            raise ValidationError("{} 包含无效选项".format(field))
        item[field] = value
    for field in DATE_FIELDS + ['origin_reviewed_at']:
        valid_date(item[field], field)
        if item[field] and not (field in {'occurred_at', 'effective_at'} and re.fullmatch(r'\d{4}-\d{2}-\d{2}', item[field])):
            from newsdesk.publication_time import utc_timestamp, PublicationTimeError
            try:
                item[field] = utc_timestamp(item[field])
            except PublicationTimeError as error:
                raise ValidationError('{}：{}'.format(field, error))
    if item["source_url"]:
        try:
            parsed = urlsplit(item["source_url"])
        except ValueError:
            raise ValidationError("来源链接格式无效")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValidationError("来源链接必须是 http 或 https 网址")
    is_demo = merged.get("is_demo", False)
    if not isinstance(is_demo, bool):
        raise ValidationError("is_demo 必须是布尔值")
    item["is_demo"] = is_demo
    if "agent_review" in merged:
        review = merged["agent_review"]
        review_categories = item["categories"]
        if "agent_review" not in data and isinstance(review, dict):
            # A person's later classification edit does not constitute another
            # agent review. Keep the original reasons and review timestamp as
            # historical evidence, even if current categories have changed.
            reasons = review.get("category_reasons", {})
            review_categories = list(reasons) if isinstance(reasons, dict) else []
            if any(category not in category_ids for category in review_categories):
                raise ValidationError("原 agent_review 包含无效的历史分类")
        item["agent_review"] = validate_agent_review(review, review_categories)
    if "ingestion" in merged:
        ingestion = dict((existing or {}).get("ingestion", {}))
        supplied = data.get("ingestion", merged["ingestion"])
        if not isinstance(supplied, dict):
            raise ValidationError("ingestion 必须是对象")
        ingestion.update(supplied)
        item["ingestion"] = validate_ingestion(ingestion)
        if existing and existing.get('ingestion') and 'published_at' in data and 'ingestion' not in data:
            from newsdesk.publication_time import iso
            if iso(item['published_at']) != iso(existing.get('published_at')):
                raise ValidationError('首次发布时间必须与来源核验结果一致，请通过重新采集修正')
    identifier = merged.get("id") or str(uuid.uuid4())
    try:
        identifier = str(uuid.UUID(identifier))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("新闻 id 必须是有效的 UUID")
    item["id"] = identifier
    timestamp = now_iso()
    item["created_at"] = (existing or {}).get("created_at", timestamp)
    item["updated_at"] = timestamp
    if preserve_timestamps:
        for field in ["created_at", "updated_at"]:
            # Full backups may explicitly restore timestamps; a partial import
            # keeps the existing creation time and records a fresh update time.
            value = data.get(field) or item[field]
            if not isinstance(value, str):
                raise ValidationError("{} 必须是日期文本".format(field))
            valid_date(value, field)
            item[field] = value
    return item


def demo_news():
    examples = [
        ("supply", "产油国联盟讨论延长减产安排", "关注正式决议与成员国实际执行，分别记录配额、产量和出口。", ["供应数量", "风险溢价"], ["Brent", "WTI"], "偏多", "提议", "产油国", "80", "50", "万桶/日", "减产规模若超过事前预期，可能收紧未来供应；需继续核验执行情况。"),
        ("demand", "航空出行指标回升，需求预期出现修正", "航班活动只能作为需求线索，还需与航煤消费和库存数据交叉验证。", ["消费需求"], ["Brent", "SC"], "偏多", "宣布", "亚洲", "4.2", "2.5", "%", "活动指标强于事前预期，可能支持航煤及原油需求。"),
        ("inventory", "商业原油库存下降，去库幅度弱于预期", "示例实际去库存 200 万桶，事前预期去库存 500 万桶，不能仅凭去库就认定利多。", ["库存", "供应数量"], ["WTI"], "偏空", "宣布", "美国", "-200", "-500", "万桶", "去库弱于预期，需结合进口、出口和炼厂加工变化判断。"),
        ("refining", "沿海炼厂计划检修，原油加工需求暂时下降", "检修影响原油采购，同时可能减少成品油供应，两者方向需要分开记录。", ["炼厂加工", "消费需求"], ["WTI", "Brent"], "混合", "宣布", "沿海炼化区", "30", "", "万桶/日", "炼厂停工可能压低当地原油需求，但对成品油供应形成收缩。"),
        ("geopolitics", "出口限制进入谈判阶段，实际影响仍待确认", "对政策表态、正式文件、执行时间和实际贸易流分别留档。", ["供应数量", "风险溢价", "运输交付"], ["Brent", "WTI", "SC"], "不确定", "提议", "跨地区", "", "", "", "谈判消息可能改变风险溢价，尚不能确认实际出口损失。"),
        ("logistics", "航线绕行增加，交付周期与保险报价上升", "跟踪港口、航线和船型，区分运输延迟与永久供应损失。", ["运输交付", "风险溢价"], ["Brent", "SC"], "偏多", "实施", "国际海运", "5", "", "天", "绕行提高交付成本，可能首先影响近月与地区价差。"),
        ("macro", "央行措辞偏紧，美元与需求预期共同变化", "记录政策相对预期的变化，避免将所有加息新闻机械标为同一方向。", ["汇率利率", "消费需求", "资金交易"], ["WTI", "Brent", "SC"], "混合", "宣布", "全球", "", "", "", "汇率、增长预期与风险偏好的作用可能相互抵消，方向暂不确定。"),
        ("policy", "进口配额方案调整，关注生效与采购节奏", "进口配额变化不等于实际进口变化，需继续跟踪炼厂采购和到港。", ["消费需求", "炼厂加工", "运输交付"], ["SC", "Brent"], "不确定", "提议", "中国", "", "", "", "政策可能影响原油采购，但幅度取决于需求与炼厂运行。"),
        ("positioning", "基金净持仓变化，关注拥挤度与报告时滞", "将仓位统计日与报告发布日期分开保存，发布前不应使用尚不可得的数据。", ["资金交易"], ["WTI", "Brent"], "不确定", "宣布", "期货市场", "", "", "手", "仓位变化是市场状态线索，不能单独推断未来价格方向。"),
        ("weather", "强风预警覆盖能源设施，等待实际停产确认", "同时记录油田、炼厂和港口暴露；原油供给与加工需求可能同时减少。", ["供应数量", "炼厂加工", "运输交付"], ["WTI"], "混合", "宣布", "沿海能源区", "", "", "", "灾害对原油供需两侧均有影响，需要按设施拆分验证。"),
    ]
    result = []
    for index, example in enumerate(examples):
        category, title, summary, channels, assets, direction, stage, region, actual, expected, unit, rationale = example
        published = "2026-10-28T{:02d}:{}:00+08:00".format(15 - index // 2, "30" if index % 2 == 0 else "00")
        result.append(validate_news({
            "title": title, "summary": "【演示数据】" + summary,
            "body": "本条是虚构的界面演示记录，不对应真实新闻、真实事件或投资结论。\n\n" + summary + "\n\n研究记录：\n1. 核验原始信息来源与首次发布时间。\n2. 比较事前预期与实际变化。\n3. 后续更新沿用同一事件编号，避免重复计数。",
            "category": category, "channels": channels, "assets": assets,
            "region": region, "entities": ["演示研究对象"], "source_name": "演示资料 · 非真实新闻",
            "source_type": "数据发布" if category in {"inventory", "positioning", "demand"} else "事实公告",
            "published_at": published, "occurred_at": published, "collected_at": published,
            "stage": stage, "actual_value": actual, "expected_value": expected, "unit": unit,
            "scale": actual, "duration": "预计一周，待核验" if index % 2 == 0 else "尚待确认",
            "horizon": "数周" if category in {"policy", "supply"} else "1–5个交易日",
            "direction": direction, "rationale": rationale, "verification": "待核验",
            "confidence": "中" if index % 3 else "低", "event_id": "DEMO-20261028-{:02d}".format(index + 1),
            "update_note": "初始演示记录；请用真实信息替换。", "is_demo": True,
        }))
    return result


class NewsStore:
    def __init__(self, path=DEFAULT_DATABASE, seed_demo=True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS news (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            initialized = connection.execute("SELECT value FROM metadata WHERE key='initialized'").fetchone()
            if not initialized:
                if seed_demo:
                    for item in demo_news():
                        self.write(connection, item)
                connection.execute("INSERT INTO metadata(key,value) VALUES('initialized',?)", (now_iso(),))

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def write(connection, item):
        from newsdesk.publication_time import PublicationTimeError, require_verified_publication, utc_timestamp
        try:
            if not item.get('is_demo'):
                item['published_at'] = (require_verified_publication(item) if item.get('ingestion')
                                        else utc_timestamp(item.get('published_at')))
            for field in DATE_FIELDS + ['created_at', 'updated_at', 'origin_reviewed_at']:
                if item.get(field):
                    # Event/effective calendar dates are not publication clocks.
                    if field in {'occurred_at', 'effective_at'} and re.fullmatch(r'\d{4}-\d{2}-\d{2}', item[field]):
                        continue
                    item[field] = utc_timestamp(item[field])
            for section, field in [('agent_review','reviewed_at'), ('ingestion','timezone_checked_at')]:
                if item.get(section, {}).get(field):
                    item[section][field] = utc_timestamp(item[section][field])
        except PublicationTimeError as error:
            raise ValidationError(str(error))
        # Workbook tiers are derived from the original issuer, never accepted
        # from an agent response or copied from the collecting news feed.
        item['origin_authority'] = assess_authority(item)
        connection.execute(
            "INSERT INTO news(id,payload,updated_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
            (item["id"], json.dumps(item, ensure_ascii=False), item["updated_at"]),
        )

    def all(self):
        with self.connect() as connection:
            rows = connection.execute("SELECT payload FROM news ORDER BY updated_at DESC, id").fetchall()
        return [enrich_authority(category_fallback(json.loads(row[0]))) for row in rows]

    def get(self, identifier):
        with self.connect() as connection:
            row = connection.execute("SELECT payload FROM news WHERE id=?", (identifier,)).fetchone()
        return enrich_authority(category_fallback(json.loads(row[0]))) if row else None

    def save(self, data, identifier=None):
        existing = self.get(identifier) if identifier else None
        if identifier and existing is None:
            raise KeyError(identifier)
        if identifier:
            data = dict(data, id=identifier)
        item = validate_news(data, existing)
        if not identifier and self.get(item["id"]):
            raise ValidationError("该新闻 id 已存在，请使用更新接口")
        with self.connect() as connection:
            self.write(connection, item)
        return item

    def delete(self, identifier):
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM news WHERE id=?", (identifier,))
            return cursor.rowcount > 0

    def import_items(self, items):
        if not isinstance(items, list) or len(items) > 10000:
            raise ValidationError("items 必须是最多 10000 条新闻的数组")
        with self.connect() as connection:
            # Validate against one locked snapshot. No concurrent writer can
            # change a record between merging the partial import and saving it.
            connection.execute("BEGIN IMMEDIATE")
            validated = []
            seen = set()
            for index, data in enumerate(items):
                existing = None
                if isinstance(data, dict) and data.get("id"):
                    try:
                        identifier = str(uuid.UUID(data["id"]))
                        row = connection.execute("SELECT payload FROM news WHERE id=?", (identifier,)).fetchone()
                        existing = json.loads(row[0]) if row else None
                    except (ValueError, TypeError, AttributeError):
                        pass  # validate_news supplies the actionable UUID error.
                try:
                    item = validate_news(data, existing, preserve_timestamps=True)
                except ValidationError as error:
                    raise ValidationError("第 {} 条：{}".format(index + 1, error))
                if item["id"] in seen:
                    raise ValidationError("第 {} 条：同一批导入不能包含重复的新闻 id，请合并后重试".format(index + 1))
                seen.add(item["id"])
                validated.append(item)
            for item in validated:
                self.write(connection, item)
        return len(validated)

    def clear_demo(self):
        identifiers = [item["id"] for item in self.all() if item.get("is_demo")]
        with self.connect() as connection:
            connection.executemany("DELETE FROM news WHERE id=?", [(identifier,) for identifier in identifiers])
        return len(identifiers)


class NewsHandler(BaseHTTPRequestHandler):
    server_version = "OilSignal/1.0"

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
            raise ValidationError("Content-Length 无效")
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValidationError("请求内容过大，最大允许 8 MB")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, UnicodeDecodeError):
            raise ValidationError("请求内容不是有效的 JSON")
        if not isinstance(data, dict):
            raise ValidationError("请求必须是 JSON 对象")
        return data

    def do_GET(self):
        if not self.check_request():
            return
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/config":
            self.json_response({"categories": CATEGORIES, "channels": CHANNELS, "assets": ASSETS, **ENUMS, "storage": "SQLite · 本地项目", "version": 1})
        elif path == "/api/settings":
            self.json_response({**self.server.updates.snapshot(),
                                "live_predictions": self.server.live_predictions.snapshot()})
        elif path == "/api/sources/health":
            self.json_response(self.server.sources.snapshot())
        elif path == "/api/research":
            from newsdesk.environment_policy import memory_environment
            catalog = self.server.evaluations.catalog()
            catalog["environments"] = list(catalog.get("environments", [])) + [memory_environment()]
            self.json_response({"benchmark": self.server.benchmarks.summary(),
                                "benchmarks": self.server.benchmarks.catalog(),
                                **catalog, "memory_benchmark": self.server.memory_evaluations.catalog()})
        elif path == "/api/trading-return":
            from newsdesk.trading_return import catalog
            self.json_response(catalog())
        elif path == "/api/trading-return/experiments":
            self.json_response(self.server.experiments.catalog())
        elif path == "/api/trading-return/experiments/agent":
            self.json_response(self.server.experiments.agent_status())
        elif path.startswith("/api/trading-return/experiments/"):
            identifier = unquote(path[len("/api/trading-return/experiments/"):])
            if not identifier or "/" in identifier:
                self.json_response({"error": "实验编号无效"}, 400)
                return
            try:
                self.json_response(self.server.experiments.detail(identifier))
            except KeyError:
                self.json_response({"error": "实验不存在"}, 404)
        elif path == "/api/benchmark/memory-leakage" or (path == "/api/benchmark" and parse_qs(parsed.query).get("kind") == ["memory-leakage"]):
            from newsdesk.memory_leakage import load_dataset
            try:
                dataset = load_dataset()
                headers = None
                if parse_qs(parsed.query).get("download", ["0"])[0] == "1":
                    headers = {"Content-Disposition": 'attachment; filename="memory-leakage-2026.json"'}
                self.json_response(dataset, headers=headers)
            except (OSError, ValueError, KeyError, TypeError):
                self.json_response({"error": "记忆泄漏题库尚未就绪或完整性校验失败"}, 503)
        elif path == "/api/benchmark":
            query = parse_qs(parsed.query, keep_blank_values=True)
            if "kind" in query:
                from newsdesk.live_predictions import live_question_predictions, CASE_ID
                from newsdesk.benchmark import DATASET_ID
                if (query.get("kind") != ["predictions"] or query.get("mode") != ["live"] or "profile" in query
                        or len(query.get("dataset_id", [])) != 1 or len(query.get("case_id", [])) != 1
                        or not DATASET_ID.fullmatch(query["dataset_id"][0])
                        or not CASE_ID.fullmatch(query["case_id"][0]) or query["case_id"][0] in (".", "..")):
                    self.json_response({"error": "模型预测须指定 mode=live、kind=predictions 和有效的 dataset_id、case_id"}, 400)
                    return
                try:
                    self.json_response(live_question_predictions(self.server.benchmarks, self.server.evaluations,
                                                                  query["dataset_id"][0], query["case_id"][0]))
                except LookupError:
                    self.json_response({"error": "实盘题目或有效发布版本不存在"}, 404)
                except (OSError, ValueError, TypeError, RuntimeError):
                    self.json_response({"error": "模型预测或题目完整性校验失败，暂不能读取"}, 503)
                return
            mode = query.get("mode", ["backtest"])[0]
            if mode not in {"backtest", "live"}:
                self.json_response({"error": "mode 必须是 backtest 或 live"}, 400)
                return
            profile = query.get("profile", ["v1"])
            if profile not in (["v1"], ["v2"]) or profile == ["v2"] and (mode != "backtest" or len(query.get("mode", ["backtest"])) != 1):
                self.json_response({"error": "profile 必须是 v1 或 v2，v2 仅支持 mode=backtest"}, 400)
                return
            history = query.get("history", ["0"])
            if history not in (["0"], ["1"]) or history == ["1"] and (mode != "live" or len(query.get("mode", [])) != 1):
                self.json_response({"error": "history 必须为 0 或 1，累计历史仅支持 mode=live"}, 400)
                return
            kwargs = {"mode": mode}
            if profile == ["v2"]:
                kwargs["profile"] = "v2"
            self.json_response(self.server.benchmarks.live_history_summary() if history == ["1"] else self.server.benchmarks.summary(**kwargs))
        elif path == "/api/benchmark/questions":
            query = parse_qs(parsed.query, keep_blank_values=True)
            family = query.get("family", [None])[0]
            mode = query.get("mode", ["backtest"])[0]
            if family is not None and family not in ASSETS:
                self.json_response({"error": "未知的 Benchmark 品类"}, 400)
                return
            if mode not in {"backtest", "live"}:
                self.json_response({"error": "mode 必须是 backtest 或 live"}, 400)
                return
            profile = query.get("profile", ["v1"])
            if profile not in (["v1"], ["v2"]) or profile == ["v2"] and (mode != "backtest" or len(query.get("mode", ["backtest"])) != 1):
                self.json_response({"error": "profile 必须是 v1 或 v2，v2 仅支持 mode=backtest"}, 400)
                return
            history = query.get("history", ["0"])
            if history not in (["0"], ["1"]) or history == ["1"] and (mode != "live" or len(query.get("mode", [])) != 1):
                self.json_response({"error": "history 必须为 0 或 1，累计历史仅支持 mode=live"}, 400)
                return
            if history == ["1"]:
                result = self.server.benchmarks.live_history()
                items = [item for item in result["questions"] if family is None or item["family"] == family]
                self.json_response({"items": items, "total": len(items), "mode": mode, "history": True,
                                    "history_revision": result["summary"]["history_revision"], "errors": result["summary"]["errors"]})
                return
            kwargs = {"family": family, "mode": mode}
            if profile == ["v2"]:
                kwargs["profile"] = "v2"
            items = self.server.benchmarks.questions(**kwargs)
            self.json_response({"items": items, "total": len(items), "mode": mode,
                                **({"profile": "v2"} if profile == ["v2"] else {})})
        elif path == "/api/evaluations":
            memory = parse_qs(parsed.query).get("benchmark", [""])[0] == "memory-leakage"
            self.json_response({"items": self.server.memory_evaluations.catalog()["runs"] if memory else self.server.evaluations.runs()})
        elif path.startswith("/api/evaluations/") and "/cases/" in path and path.endswith("/trace"):
            segments = path.split("/")
            if len(segments) != 7 or segments[4] != "cases":
                self.json_response({"error": "交互记录不存在"}, 404)
                return
            run_id, case_id = unquote(segments[3]), unquote(segments[5])
            try:
                manager = self.server.memory_evaluations if run_id.startswith("memory-") else self.server.evaluations
                transcript = manager.get_case_trace(run_id, case_id)
                headers = None
                if parse_qs(parsed.query).get("download", ["0"])[0] == "1":
                    # Both identifiers are validated by the manager before they
                    # can enter a filename or select any on-disk artifact.
                    headers = {"Content-Disposition": 'attachment; filename="{}-{}-trace.json"'.format("memory" if run_id.startswith("memory-") else "env01", case_id[:120])}
                self.json_response(transcript, headers=headers)
            except (KeyError, FileNotFoundError, ValueError):
                self.json_response({"error": "该题尚无可用交互记录"}, 404)
        elif path.startswith("/api/evaluations/"):
            identifier = unquote(path[len("/api/evaluations/"):])
            try:
                manager = self.server.memory_evaluations if identifier.startswith("memory-") else self.server.evaluations
                self.json_response(manager.get_run(identifier))
            except (KeyError, FileNotFoundError, ValueError):
                self.json_response({"error": "评估记录不存在"}, 404)
        elif path == "/api/market/contracts":
            self.json_response(self.server.market_data.catalog())
        elif path.startswith("/api/market/") and path.endswith("/indicators"):
            identifier = unquote(path[len("/api/market/"):-len("/indicators")])
            query = parse_qs(parsed.query)
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
            try:
                self.json_response(self.server.market_indicators.get(
                    identifier, **options,
                ))
            except MarketDataError as error:
                self.json_response({"error": error.message, "source": error.source, "contract_id": error.contract_id}, error.status)
        elif path.startswith("/api/market/"):
            identifier = unquote(path[len("/api/market/"):])
            query = parse_qs(parsed.query)
            try:
                data = self.server.market_data.get(
                    identifier,
                    period=query.get("period", ["1d"])[0],
                    refresh=query.get("refresh", ["0"])[0] == "1",
                )
                self.json_response(data)
            except MarketDataError as error:
                self.json_response({"error": error.message, "source": error.source, "contract_id": error.contract_id}, error.status)
        elif path == "/api/source-coverage":
            from newsdesk.source_authority import coverage
            source_registry = json.loads((ROOT / 'source_registry.json').read_text(encoding='utf-8'))
            settings_path = ROOT / 'data' / 'settings.json'
            settings = json.loads(settings_path.read_text(encoding='utf-8')) if settings_path.exists() else {}
            report = coverage(self.server.store.all(), source_registry, settings.get('jobs', {}).get('news', {}).get('summary'))
            self.json_response(dict(report, checked_at=now_iso()))
        elif path == "/api/news":
            items = self.server.store.all()
            query = parse_qs(parsed.query)
            if query.get("include_excluded", ["0"])[0] != "1":
                items = [item for item in items if item.get("agent_review", {}).get("relevance") is not False]
            if query.get("category"):
                items = [item for item in items if query["category"][0] in item["categories"]]
            for field in ["direction", "verification", "source_type"]:
                if query.get(field):
                    items = [item for item in items if item.get(field) == query[field][0]]
            if query.get("asset"):
                items = [item for item in items if query["asset"][0] in item["assets"]]
            if query.get("q"):
                needle = query["q"][0].casefold()
                items = [item for item in items if needle in json.dumps(item, ensure_ascii=False).casefold()]
            self.json_response({"items": items, "total": len(items)})
        elif path == "/api/export":
            self.json_response({"version": 1, "exported_at": now_iso(), "items": self.server.store.all()}, headers={"Content-Disposition": 'attachment; filename="oil-signal-news.json"'})
        elif path.startswith("/api/news/"):
            item = self.server.store.get(unquote(path[len("/api/news/"):]))
            self.json_response(item if item else {"error": "新闻不存在"}, 200 if item else 404)
        elif path.startswith("/api/"):
            self.json_response({"error": "接口不存在"}, 404)
        else:
            self.serve_static(parsed.path)

    def do_HEAD(self):
        self.do_GET()

    def serve_static(self, request_path):
        try:
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
        except (OSError, ValueError):
            self.json_response({"error": "文件不存在"}, 404)

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
            if method == "POST" and path == "/api/benchmark/build":
                if set(data) - {"days", "refresh", "mode", "profile"}:
                    raise ValidationError("构建参数仅支持 days、refresh、mode、profile")
                days = data.get("days", 30)
                refresh = data.get("refresh", False)
                mode = data.get("mode", "backtest")
                profile = data.get("profile", "v1")
                if type(days) is not int or not 1 <= days <= 90 or not isinstance(refresh, bool):
                    raise ValidationError("days 必须是 1–90 的整数，refresh 必须是布尔值")
                if mode not in {"backtest", "live"}:
                    raise ValidationError("mode 必须是 backtest 或 live")
                if profile not in ("v1", "v2") or profile == "v2" and mode != "backtest":
                    raise ValidationError("profile 必须是 v1 或 v2，v2 仅支持 mode=backtest")
                try:
                    kwargs = {"days": days, "refresh": refresh, "mode": mode}
                    if profile == "v2":
                        kwargs["profile"] = "v2"
                    result = self.server.benchmarks.build(**kwargs)
                    if mode == "live":
                        self.server._notify_live_published()
                    self.json_response(result, 201)
                except (ValueError, RuntimeError) as error:
                    raise ValidationError(str(error))
            elif method == "POST" and path == "/api/trading-return/model-test":
                if set(data) != {"model"} or not isinstance(data.get("model"), str):
                    raise ValidationError("模型测试只接受 model 字段")
                from newsdesk.trading_return import probe_model
                try:
                    self.json_response(probe_model(data["model"]))
                except ValueError as error:
                    raise ValidationError(str(error))
            elif method == "POST" and path in ("/api/trading-return/env-test", "/api/trading-return/evaluator-test"):
                allowed = {"model", "question", "prediction", "actual", "environment", "bars", "decisions",
                           "market", "initial_capital", "slippage_scenario"}
                if set(data) - allowed or "model" not in data:
                    raise ValidationError("Env Server 测试需要 model")
                from newsdesk.trading_return import run_evaluator_test
                try:
                    self.json_response(run_evaluator_test(data.get("model"), data.get("question", ""),
                                                          data.get("prediction"), data.get("actual"),
                                                          data.get("environment", "env-v1"), bars=data.get("bars"),
                                                          decisions=data.get("decisions"), market=data.get("market", "SC"),
                                                          initial_capital=data.get("initial_capital"),
                                                          slippage_scenario=data.get("slippage_scenario", "base")))
                except ValueError as error:
                    raise ValidationError(str(error))
            elif method == "POST" and path == "/api/trading-return/mcp-test":
                allowed = {"server", "question", "cutoff", "keywords", "limit", "family"}
                if set(data) - allowed or not {"server", "question", "cutoff", "keywords"}.issubset(data):
                    raise ValidationError("MCP 测试需要 server、question、cutoff 和 keywords")
                from newsdesk.trading_return import run_mcp_test
                try:
                    result = run_mcp_test(data.get("server"), data.get("question"), data.get("cutoff"),
                                          data.get("keywords"), data.get("limit", 5), data.get("family", "WTI"),
                                          store=self.server.store)
                    self.json_response(result)
                except ValueError as error:
                    raise ValidationError(str(error))
                except (OSError, RuntimeError, KeyError, TypeError):
                    self.json_response({"error": "MCP 测试暂不可用，请稍后重试"}, 503)
            elif method == "POST" and path == "/api/trading-return/experiments":
                allowed = {"name", "submitter", "models", "mcp", "environment", "evaluator", "bmk", "strategy_doc", "prompt"}
                required = {"name", "submitter", "models", "mcp", "bmk", "strategy_doc"}
                if set(data) - allowed or not required.issubset(data):
                    raise ValidationError("创建实验需要名称、提交人、模型、MCP、Env、BMK 和策略文档")
                try:
                    self.json_response(self.server.experiments.create(data), 201)
                except ValueError as error:
                    raise ValidationError(str(error))
            elif method == "DELETE" and path.startswith("/api/trading-return/experiments/"):
                identifier = unquote(path[len("/api/trading-return/experiments/"):])
                if not identifier or "/" in identifier:
                    raise ValidationError("实验编号无效")
                try:
                    self.server.experiments.delete(identifier)
                except KeyError:
                    self.json_response({"error": "实验不存在"}, 404)
                else:
                    self.json_response({"ok": True})
            elif method == "POST" and path.startswith("/api/trading-return/experiments/") and path.endswith("/resume"):
                identifier = unquote(path[len("/api/trading-return/experiments/"):-len("/resume")].rstrip("/"))
                if not identifier or "/" in identifier or data:
                    raise ValidationError("实验编号无效")
                try:
                    self.json_response(self.server.experiments.resume_evaluations(identifier))
                except KeyError:
                    self.json_response({"error": "实验不存在"}, 404)
                except (RuntimeError, ValueError) as error:
                    raise ValidationError(str(error))
            elif method == "POST" and path.startswith("/api/trading-return/experiments/") and path.endswith("/stop-live"):
                identifier = unquote(path[len("/api/trading-return/experiments/"):-len("/stop-live")].rstrip("/"))
                if not identifier or "/" in identifier or data:
                    raise ValidationError("实验编号无效")
                try:
                    self.json_response(self.server.experiments.stop_live(identifier))
                except KeyError:
                    self.json_response({"error": "实验不存在"}, 404)
            elif method == "POST" and path == "/api/evaluations":
                if set(data) - {"strategy_id", "environment_id", "dataset_id", "workers", "mode", "family", "one_day", "input_date"}:
                    raise ValidationError("评估参数包含未知字段")
                workers = data.get("workers", 4)
                if type(workers) is not int or not 1 <= workers <= 6:
                    raise ValidationError("workers 必须是 1–6 的整数")
                mode, family = data.get("mode", "backtest"), data.get("family")
                if mode not in {"backtest", "live"} or (family is not None and family not in ASSETS):
                    raise ValidationError("未知的评估模式或期货品类")
                one_day, input_date = data.get("one_day", False), data.get("input_date")
                if type(one_day) is not bool or (input_date is not None and not isinstance(input_date, str)):
                    raise ValidationError("one_day 必须是布尔值，input_date 必须是 YYYY-MM-DD")
                try:
                    start_kwargs = {"strategy_id": data.get("strategy_id", "gpt6.1-sol"),
                                    "environment_id": data.get("environment_id", "env01"),
                                    "dataset_id": data.get("dataset_id"), "workers": workers,
                                    "mode": mode, "family": family}
                    if one_day or input_date is not None:
                        start_kwargs.update(one_day=one_day, input_date=input_date)
                    result = self.server.evaluations.start(**start_kwargs)
                    self.json_response(result, 202)
                except ValueError as error:
                    raise ValidationError(str(error))
                except RuntimeError as error:
                    self.json_response({"error": str(error)}, 409)
                except FileNotFoundError:
                    raise ValidationError("请先构建对应模式的主连 Benchmark")
            elif method == "POST" and path.startswith("/api/evaluations/") and path.endswith("/settle"):
                if set(data) - {"refresh"} or type(data.get("refresh", True)) is not bool:
                    raise ValidationError("结算参数只接受 refresh 布尔值")
                identifier = unquote(path[len("/api/evaluations/"):-len("/settle")])
                try:
                    self.json_response(self.server.evaluations.settle(identifier, refresh=data.get("refresh", True)))
                except KeyError:
                    self.json_response({"error": "评估记录不存在"}, 404)
                except (ValueError, RuntimeError) as error:
                    raise ValidationError(str(error))
            elif method == "POST" and path == "/api/sources/check":
                ids = data.get('source_ids')
                if set(data) - {'source_ids'} or (ids is not None and (not isinstance(ids,list) or not all(isinstance(s,str) for s in ids))):
                    raise ValidationError('source_ids 必须是信源编号列表')
                try:
                    started = self.server.sources.trigger(ids)
                except ValueError as error:
                    raise ValidationError(str(error))
                result = self.server.sources.snapshot()
                if not started: result['error'] = '检测正在进行，请等待完成'
                self.json_response(result,202 if started else 409)
            elif method == "PUT" and path == "/api/settings":
                try:
                    self.json_response(self.server.updates.configure(data))
                except ValueError as error:
                    raise ValidationError(str(error))
            elif method == "POST" and path in {"/api/updates/news", "/api/updates/contracts"}:
                if data:
                    raise ValidationError("手动更新无需额外参数，请提交空对象")
                name = path.rsplit("/", 1)[-1]
                started = self.server.updates.trigger(name)
                result = self.server.updates.snapshot()
                if not started:
                    result["error"] = "该更新任务已在运行，请等待本轮完成"
                self.json_response(result, 202 if started else 409)
            elif method == "POST" and path == "/api/news":
                self.json_response(self.server.store.save(data), 201)
            elif method == "POST" and path == "/api/import":
                count = self.server.store.import_items(data.get("items"))
                self.json_response({"imported": count})
            elif method == "POST" and path == "/api/demo/clear":
                self.json_response({"ok": True, "deleted": self.server.store.clear_demo()})
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
        except ValidationError as error:
            self.json_response({"error": str(error)}, 400)
        except KeyError:
            self.json_response({"error": "新闻不存在"}, 404)
        except sqlite3.Error:
            self.json_response({"error": "本地数据库写入失败，请稍后重试"}, 500)
        except OSError:
            self.json_response({"error": "本地更新设置写入失败，请检查项目数据目录"}, 500)


class LocalNewsServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        from newsdesk.benchmark import BenchmarkStore
        from newsdesk.daily_update import run_futures_update
        from newsdesk.evaluation import EvaluationManager
        from newsdesk.memory_evaluation import MemoryEvaluationManager
        from newsdesk.live_prediction_auto import AutomaticLivePredictions
        from newsdesk.experiments import ExperimentStore

        self.store = store
        from newsdesk.source_maintenance import SourceMaintenance
        self.sources = SourceMaintenance(store.path.parent)
        self.market_data = MarketData(cache_dir=store.path.parent / "market")
        self.market_indicators = MarketIndicators(
            self.market_data, SupplementalData(cache_dir=store.path.parent / "market" / "supplemental"),
        )
        self.benchmarks = BenchmarkStore(store.path.parent / "benchmark", self.market_data)
        self.evaluations = EvaluationManager(store.path.parent / "evaluations", self.benchmarks, store)
        self.memory_evaluations = MemoryEvaluationManager(store.path.parent / "memory-evaluations")
        self.live_predictions = AutomaticLivePredictions(self.evaluations.root, self.benchmarks, store.path)
        self.experiments = ExperimentStore(store.path.parent, self.benchmarks, store.path, self.evaluations)
        self.updates = UpdateScheduler(store.path.parent / "settings.json", {
            "news": lambda: run_news_update(store.path, store.path.parent / "ingestion", self.updates._stop),
            "contracts": lambda: run_futures_update(self.market_data, self.benchmarks, self.evaluations,
                                                    on_live_published=self._notify_live_published),
        })
        super().__init__(address, NewsHandler)

    def _notify_live_published(self):
        experiment_result = self.experiments.notify_live_published()
        prediction_result = self.live_predictions.notify()
        return {"status": "scheduled", "experiments": experiment_result, "predictions": prediction_result}

    def serve_forever(self, poll_interval=0.5):
        self.live_predictions.start()
        self.experiments.start()
        self.updates.start()
        return super().serve_forever(poll_interval=poll_interval)

    def server_close(self):
        self.live_predictions.stop()
        self.experiments.stop()
        self.updates.stop()
        self.market_indicators.close()
        super().server_close()


def create_server(host="127.0.0.1", port=8765, database=DEFAULT_DATABASE, seed_demo=True):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("服务只允许绑定 127.0.0.1 或 localhost")
    return LocalNewsServer((host, port), NewsStore(database, seed_demo=seed_demo))


def main():
    parser = argparse.ArgumentParser(description="原油新闻本地研究台")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args()
    try:
        load_private_aihub_config()
        server = create_server(args.host, args.port, args.database)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, "无法启动本地服务：{}\n".format(error))
    def stop_service(signum, frame):
        # SIGTERM is used when restarting the detached localhost service. Route
        # it through the same cleanup as Ctrl+C, including the news subprocess.
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_service)
    print("OIL / SIGNAL · 原油新闻研究台", flush=True)
    print("Local: http://localhost:{}/".format(server.server_port), flush=True)
    print("Database: {}".format(args.database.resolve()), flush=True)
    print("按 Ctrl+C 停止服务。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n本地服务已停止。", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
