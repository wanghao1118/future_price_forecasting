"""Build the isolated gold news database from public financial feeds.

Collection is deliberately separate from the oil database. Public candidates
are fetched first, then a conservative gold-topic screen assigns one or more
research categories. Records retain their source URL and UTC timestamps and
start with verification=待核验; this command never invents publication times.
"""
import argparse
import hashlib
import json
import re
import html
import sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from golddesk import server as gold_server

# Direct gold terms are retained for source-level traceability.  Gold futures
# also react to the same macro, risk, flow, and cross-commodity shocks used by
# the oil desk, so the inclusion screen intentionally admits those drivers.
GOLD_TERMS = re.compile(r"黄金|金价|金矿|贵金属|黄金期货|沪金|上期所黄金|金条|金币|央行购金|黄金储备|黄金ETF|gold|bullion|precious\s+metal|xau|comex\s+gold|shfe\s+gold", re.I)
GOLD_DRIVER_TERMS = re.compile(
    r"美联储|联储|FOMC|利率|降息|加息|通胀|CPI|PCE|非农|就业|美元|美元指数|DXY|美债|国债|债券收益率|收益率|流动性|经济数据|GDP|央行|货币政策|联邦基金|银行危机|衰退|"
    r"战争|冲突|制裁|地缘|中东|乌克兰|俄罗斯|伊朗|以色列|巴以|红海|台海|避险|风险偏好|risk.?off|risk.?on|geopolit|sanction|safe.?haven|"
    r"ETF|持仓|基金|保证金|限仓|净多|净空|仓位|资金流|波动率|VIX|期权|position|fund|margin|futures|volatility|options|"
    r"原油|油价|能源|OPEC|欧佩克|白银|铂|大宗商品|commodity|crude|oil|silver|platinum|"
    r"yield|dollar|inflation|rate|fed|central\s+bank|treasury|employment|payroll|recession|banking\s+crisis",
    re.I,
)
GOLD_RELEVANCE_TERMS = re.compile(
    r"(?:" + GOLD_TERMS.pattern + r")|(?:" + GOLD_DRIVER_TERMS.pattern + r")", re.I
)
CATEGORY_RULES = {
    "supply": re.compile(r"金矿|矿山|产量|矿产|供应|供给|mine|production|supply", re.I),
    "demand": re.compile(r"珠宝|首饰|金条|金币|消费|需求|工业用金|jewell|bar demand|coin demand", re.I),
    "inventory": re.compile(r"库存|仓单|现货|交割|COMEX库存|上海金库|stock|warehouse|delivery", re.I),
    "refining": re.compile(r"精炼|冶炼|回收|再生金|refin|recycl", re.I),
    "geopolitics": re.compile(r"战争|冲突|制裁|避险|地缘|中东|乌克兰|geopolit|sanction|safe.?haven", re.I),
    "logistics": re.compile(r"运输|贸易|进口|出口|港口|交付|关税|trade|import|export|ship", re.I),
    "macro": re.compile(r"美联储|利率|降息|加息|美元|通胀|非农|经济|债券|流动性|fed|rate|dollar|inflation|yield", re.I),
    "policy": re.compile(r"央行|储备|官方|货币政策|监管|购金|central bank|reserve|official", re.I),
    "positioning": re.compile(r"ETF|持仓|基金|保证金|限仓|期货|净多|净空|仓位|position|fund|margin|futures", re.I),
    "weather": re.compile(r"天气|洪水|地震|暴雨|灾害|停产|电力|weather|flood|earthquake|outage", re.I),
}
DIRECTION_RULES = [
    ("偏多", re.compile(r"上涨|上升|走强|支撑|增加|回升|降息|宽松|避险|买入|rise|rall|support|increase|cut", re.I)),
    ("偏空", re.compile(r"下跌|下降|走弱|承压|减少|回落|加息|收紧|抛售|跌破|fall|drop|weaken|sell|hike", re.I)),
]


RSS_FEEDS = {
    "google-news-en": "https://news.google.com/rss/search?q=gold+futures&hl=en-US&gl=US&ceid=US:en",
    "google-news-zh": "https://news.google.com/rss/search?q=%E9%BB%84%E9%87%91+%E6%9C%9F%E8%B4%A7&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
}


def _strip_html(value):
    value = html.unescape(str(value or ""))
    return re.sub(r"<[^>]+>", " ", value).replace("\xa0", " ")


def crawl_rss(output_dir, limit=100):
    """Collect public RSS discovery records with exact RFC-822 timestamps."""
    destination = Path(output_dir)
    raw_dir = destination / "raw-rss"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    reports = []
    for source_id, url in RSS_FEEDS.items():
        status = {"source_id": source_id, "url": url, "status": "ok", "count": 0, "error": ""}
        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; GoldSignal/1.0)"})
            with urlopen(request, timeout=20) as response:
                raw = response.read(2 * 1024 * 1024)
            raw_path = raw_dir / (source_id + ".xml")
            raw_path.write_bytes(raw)
            root = ET.fromstring(raw)
            for item in root.findall("./channel/item")[:limit]:
                title = _strip_html(item.findtext("title"))
                body = _strip_html(item.findtext("description"))
                link = (item.findtext("link") or "").strip()
                stamp = item.findtext("pubDate") or ""
                try:
                    published = parsedate_to_datetime(stamp).astimezone(timezone.utc).isoformat(timespec="seconds")
                except (TypeError, ValueError, OverflowError):
                    published = ""
                source = item.find("source")
                source_name = (source.text or "Google News") if source is not None else "Google News"
                if not title or not link or not published:
                    continue
                rows.append({"candidate_id": source_id + ":" + hashlib.sha256(link.encode()).hexdigest()[:24],
                             "source_id": source_id, "source_name": source_name.strip(),
                             "external_id": hashlib.sha256(link.encode()).hexdigest()[:24],
                             "source_url": link, "title": title[:400], "body": body[:20000],
                             "published_at": published, "collected_at": utc_now(),
                             "raw_path": str(raw_path.resolve()), "content_scope": "public_article_excerpt"})
                status["count"] += 1
        except Exception as error:
            status.update(status="error", error="{}: {}".format(type(error).__name__, str(error)[:300]))
        reports.append(status)
    return rows, reports


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify(text):
    categories = [key for key, rule in CATEGORY_RULES.items() if rule.search(text)]
    if not categories:
        categories = ["macro"]
    direction = "不确定"
    for value, rule in DIRECTION_RULES:
        if rule.search(text):
            direction = value
            break
    return categories[:len(gold_server.CATEGORIES)], direction


def is_gold_relevant(text):
    """Return whether text contains gold or a documented gold-price driver."""
    return bool(GOLD_RELEVANCE_TERMS.search(text or ""))


def oil_source_ids():
    """Return the currently enabled source IDs from the oil workbench registry."""
    registry_path = Path(__file__).resolve().parents[1] / "newsdesk" / "source_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    return [entry["id"] for entry in registry.get("sources", []) if entry.get("enabled")]


def collect_migrated_oil_sources(output_dir, source_ids=None, limit_per_source=100, institution_limit=5):
    """Run the oil workbench's public adapters, retaining their provenance.

    The oil database is never opened or modified. Only its source registry and
    public collection adapters are reused; relevance and taxonomy are applied
    by the gold-specific code below.
    """
    from newsdesk.ingest import collect

    selected = list(source_ids) if source_ids is not None else oil_source_ids()
    crawl_dir = Path(output_dir) / "oil-source-crawl"
    manifest = collect(crawl_dir, source_ids=selected, limit=limit_per_source,
                       research_limit=20, institution_limit=institution_limit)
    candidate_path = crawl_dir / "candidates.jsonl"
    candidates = []
    if candidate_path.exists():
        for line in candidate_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                candidates.append(json.loads(line))
    return candidates, manifest


def oil_database_candidates(database=None):
    """Backfill historical gold-topic candidates already collected by oil."""
    oil_db = Path(database or Path(__file__).resolve().parents[1] / "newsdesk" / "data" / "news.sqlite3")
    if not oil_db.exists():
        return []
    candidates = []
    connection = sqlite3.connect(oil_db)
    try:
        rows = connection.execute("SELECT payload FROM news")
        for (payload,) in rows:
            item = json.loads(payload)
            title = str(item.get("title") or "")
            body = str(item.get("body") or "")
            if not is_gold_relevant(title + "\n" + body):
                continue
            ingestion = item.get("ingestion") or {}
            candidates.append({
                "candidate_id": "oil-db:" + str(item.get("id") or ""),
                "source_id": str(ingestion.get("source_id") or "oil-db"),
                "source_name": str(item.get("source_name") or ingestion.get("collection_platform") or "原油新闻库"),
                "external_id": str(ingestion.get("external_id") or item.get("id") or ""),
                "source_url": str(item.get("source_url") or ingestion.get("canonical_url") or ""),
                "title": title,
                "body": body,
                "published_at": str(item.get("published_at") or ""),
                "collected_at": str(item.get("collected_at") or item.get("updated_at") or utc_now()),
                "raw_path": str(ingestion.get("raw_path") or ""),
                "content_scope": str(ingestion.get("content_scope") or "public_flash"),
                "source_published_at": str(ingestion.get("source_published_at") or item.get("published_at") or ""),
                "source_timezone": str(ingestion.get("source_timezone") or ""),
                "timezone_status": str(ingestion.get("timezone_status") or ""),
                "timezone_evidence": str(ingestion.get("timezone_evidence") or ""),
                "original_published_at": str(ingestion.get("original_published_at") or item.get("published_at") or ""),
                "publication_precision": str(ingestion.get("publication_precision") or "minute"),
                "source_page_kind": str(ingestion.get("source_page_kind") or "public_feed"),
                "detail_access": str(ingestion.get("detail_access") or "public"),
                "public_source_url": str(ingestion.get("public_source_url") or item.get("source_url") or ""),
                "run_id": str(ingestion.get("run_id") or "oil-db-backfill"),
            })
    finally:
        connection.close()
    return candidates


def candidate_to_record(candidate, review_at):
    title = str(candidate.get("title") or "").strip()
    body = str(candidate.get("body") or "").strip()
    text = title + "\n" + body
    if not is_gold_relevant(text):
        return None
    published = str(candidate.get("published_at") or "").strip()
    # Unknown/date-only times are not admitted. The source adapter already
    # converts known source offsets/epochs to aware UTC ISO timestamps.
    if not re.search(r"T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})$", published):
        return None
    categories, direction = classify(text)
    category = categories[0]
    canonical = str(candidate.get("source_url") or "").strip()
    identifier = str(__import__('uuid').uuid5(__import__('uuid').NAMESPACE_URL, "gold:" + canonical))
    summary = re.sub(r"\s+", " ", body).strip()[:600] or title
    source_name = str(candidate.get("source_name") or candidate.get("source_id") or "公开信源")
    evidence = title if not body else body[:800]
    reasons = {key: "公开候选文本涉及黄金市场主题，先按规则归入该分类；后续可继续人工或模型复核。" for key in categories}
    raw_path_value = str(candidate.get("raw_path") or "")
    try:
        raw_sha256 = hashlib.sha256(Path(raw_path_value).read_bytes()).hexdigest() if raw_path_value else ""
    except OSError:
        raw_sha256 = ""
    source_id = str(candidate.get("source_id") or "")
    # Keep the source's declared clock in provenance while the normalized
    # record fields below remain UTC+0. The public feed adapters use Unix
    # epoch timestamps for these sources; Jin10/THS expose Asia/Shanghai
    # local timestamps; RSS candidates arrive with an RFC-822 offset that
    # has already been normalized by crawl_rss.
    source_timezone = str(candidate.get("source_timezone") or "").strip()
    timezone_status = str(candidate.get("timezone_status") or "").strip()
    if not source_timezone or not timezone_status:
        if source_id in {"cls", "jiemian", "stcn"}:
            source_timezone, timezone_status = "UTC (Unix epoch)", "verified_epoch"
        elif source_id in {"jin10", "ths"}:
            source_timezone, timezone_status = "Asia/Shanghai", "verified_site_timezone"
        else:
            source_timezone, timezone_status = "UTC (RSS offset normalized)", "verified_offset"
    source_published_at = str(candidate.get("source_published_at") or published)
    original_published_at = str(candidate.get("original_published_at") or source_published_at)
    publication_precision = str(candidate.get("publication_precision") or candidate.get("published_at_precision") or "minute")
    source_page_kind = str(candidate.get("source_page_kind") or "public_feed")
    detail_access = str(candidate.get("detail_access") or "public")
    public_source_url = str(candidate.get("public_source_url") or canonical)
    timezone_evidence = str(candidate.get("timezone_evidence") or "公开信源适配器输出的带时区时间；统一存储为 UTC+0。")
    ingestion = {
        "source_id": str(candidate.get("source_id") or ""),
        "external_id": str(candidate.get("external_id") or ""),
        "canonical_url": canonical,
        "raw_path": raw_path_value,
        "content_hash": hashlib.sha256((title + "\n" + body).encode("utf-8")).hexdigest(),
        "run_id": str(candidate.get("run_id") or "gold-public-feed"),
        "collection_platform": source_name,
        "publisher_name": source_name,
        "content_scope": str(candidate.get("content_scope") or "public_flash"),
        "published_date": published[:10],
        "publication_precision": publication_precision,
        "public_source_url": public_source_url,
        "detail_access": detail_access,
        "source_page_kind": source_page_kind,
        "source_published_at": source_published_at,
        "source_timezone": source_timezone,
        "timezone_status": timezone_status,
        "timezone_evidence": timezone_evidence,
        "timezone_checked_at": review_at,
        "timezone_audit_version": "golddesk-utc-v1",
        "original_published_at": original_published_at,
        "publication_raw_sha256": raw_sha256,
    }
    return gold_server.base.validate_news({
        "id": identifier, "title": title[:400], "summary": summary,
        "body": body[:200000], "category": category, "categories": categories,
        "channels": ["汇率利率" if key == "macro" else "矿产供应" if key == "supply" else "实物需求" if key == "demand" else "资金交易" if key == "positioning" else "避险溢价" if key == "geopolitics" else "库存仓单" if key == "inventory" else "运输交付" for key in categories],
        "assets": ["AU"], "region": "全球", "entities": [], "source_name": source_name,
        "source_url": canonical, "source_type": "事实公告", "origin_name": "", "origin_status": "unresolved",
        "origin_reason": "公开候选已入黄金库，原始发布机构尚未单独核验。", "published_at": published,
        "occurred_at": published, "collected_at": str(candidate.get("collected_at") or review_at),
        "stage": "宣布", "horizon": "1–5个交易日", "direction": direction,
        "rationale": "规则初筛：文本包含黄金主题或黄金价格驱动因素；方向仅作待核验研究标记。",
        "verification": "待核验", "confidence": "低", "event_id": "GOLD-" + published[:10].replace("-", ""),
        "update_note": "golddesk 公开信源批次；内容范围：{}。规则初筛后待人工或模型复核。".format(ingestion["content_scope"]),
        "ingestion": ingestion,
        "agent_review": {"reviewer": "golddesk-rule-review-v1", "reviewed_at": review_at,
                          "relevance": True, "relevance_reason": "公开标题或正文包含黄金主题或黄金价格驱动因素，已进入黄金候选库；未将规则命中当作事实核验。",
                          "category_reasons": reasons, "evidence": [evidence[:4000]], "confidence": "低"},
        "is_demo": False,
    })


def run(output=None, database=None, sources=None, limit_per_source=100, institution_limit=5,
        input_candidates=None):
    root = Path(__file__).resolve().parent
    database = Path(database or root / "data" / "gold.sqlite3")
    output = Path(output or root / "data" / "ingestion" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    historical_candidates = oil_database_candidates()
    if input_candidates:
        oil_candidates_path = Path(input_candidates)
        oil_candidates = [json.loads(line) for line in oil_candidates_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        source_report = {"mode": "existing-candidates", "candidates_path": str(oil_candidates_path.resolve()),
                         "candidate_count": len(oil_candidates)}
    else:
        oil_candidates, source_report = collect_migrated_oil_sources(
            output, source_ids=sources, limit_per_source=limit_per_source,
            institution_limit=institution_limit)
    candidates_path = output / "candidates.jsonl"
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    all_oil_candidates = historical_candidates + oil_candidates
    candidates_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in all_oil_candidates), encoding="utf-8")
    rss_rows, rss_reports = crawl_rss(output, limit=limit_per_source)
    with candidates_path.open("a", encoding="utf-8") as stream:
        for row in rss_rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    report = {"run_id": output.name, "source_registry": str((Path(__file__).resolve().parents[1] / "newsdesk" / "source_registry.json").resolve()),
              "oil_sources": source_report, "historical_oil_candidates": len(historical_candidates),
              "candidate_count": len(all_oil_candidates) + len(rss_rows),
              "rss_sources": rss_reports}
    (output / "crawl_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    review_at = utc_now()
    store = gold_server.base.NewsStore(database, seed_demo=False)
    saved, skipped = [], []
    for line in candidates_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        candidate = json.loads(line)
        candidate.setdefault("run_id", report.get("run_id", ""))
        try:
            item = candidate_to_record(candidate, review_at)
        except Exception as error:
            skipped.append({"candidate_id": candidate.get("candidate_id"), "reason": str(error)})
            continue
        if item is None:
            skipped.append({"candidate_id": candidate.get("candidate_id"), "reason": "未通过黄金主题/价格驱动因素或精确 UTC 首发时间准入"})
            continue
        try:
            existing = store.get(item["id"])
            if existing:
                store.save(item, item["id"])
            else:
                store.save(item)
            saved.append(item["id"])
        except Exception as error:
            skipped.append({"candidate_id": candidate.get("candidate_id"), "reason": str(error)})
    result = {"run_id": report.get("run_id"), "database": str(database.resolve()), "candidate_count": report.get("candidate_count", 0),
              "saved": len(saved), "skipped": len(skipped), "saved_ids": saved, "skipped_records": skipped,
              "timezone": "UTC+0", "review": "golddesk-rule-review-v1; verification=待核验"}
    (output / "gold-review-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description="采集并构建黄金期货新闻数据库")
    parser.add_argument("run", nargs="?", default="run", choices=["run"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--sources", nargs="*", default=None, help="可选，限制迁移的原油信源 ID；默认使用原油登记中全部启用信源")
    parser.add_argument("--limit-per-source", type=int, default=100)
    parser.add_argument("--institution-limit", type=int, default=5)
    parser.add_argument("--input-candidates", type=Path, help="复用已完成的原油信源 candidates.jsonl，不重复抓取")
    args = parser.parse_args()
    sources = args.sources
    if sources and len(sources) == 1 and "," in sources[0]:
        sources = [item for item in sources[0].split(",") if item]
    print(json.dumps(run(args.output, args.database, sources, args.limit_per_source,
                         args.institution_limit, args.input_candidates), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
