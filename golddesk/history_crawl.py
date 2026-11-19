"""Backfill 2026 gold-impact candidates through the oil workbench sources.

This wrapper uses the existing public historical media adapters from
``newsdesk.history_media``. It keeps raw responses and resumable checkpoints,
then sends the combined candidates through the gold-specific UTC filter and
SQLite builder. The end date defaults to today's Asia/Shanghai date, so a
future portion of 2026 is never presented as collected.
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from newsdesk.history_media import run_source

from golddesk.ingest import run as build_gold_database

TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_SOURCES = ["jin10", "cls", "ths", "jiemian", "stcn", "aastocks", "caixin"]
DEFAULT_GOLD_DISCOVERY_TERMS = [
    "黄金", "金价", "黄金期货", "贵金属", "金矿", "央行购金", "黄金储备",
    "黄金ETF", "金条", "金币", "伦敦金", "现货金", "bullion", "gold", "xau",
    "precious metal",
]


def today_local():
    return datetime.now(TZ).date().isoformat()


def crawl_source(source_id, start_date, end_date, root, *, max_pages=1000, max_articles=50000,
                 fetch_articles=False):
    kwargs = {
        "max_pages": max_pages,
        "max_articles": max_articles,
        "fetch_articles": fetch_articles,
    }
    if source_id == "aastocks":
        kwargs["discovery_terms"] = DEFAULT_GOLD_DISCOVERY_TERMS
    return run_source(source_id, start_date, end_date, root / source_id, **kwargs)


def collect(start_date="2026-01-01", end_date=None, output=None, sources=None,
            max_pages=1000, max_articles=50000):
    end_date = end_date or today_local()
    output = Path(output or Path(__file__).resolve().parent / "data" / ("history-" + end_date))
    media_root = output / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    selected = list(sources or DEFAULT_SOURCES)
    reports = []
    with ThreadPoolExecutor(max_workers=min(4, len(selected))) as pool:
        futures = {
            pool.submit(crawl_source, source_id, start_date, end_date, media_root,
                        max_pages=max_pages, max_articles=max_articles): source_id
            for source_id in selected
        }
        for future in as_completed(futures):
            source_id = futures[future]
            try:
                reports.append(future.result())
            except Exception as error:
                reports.append({"source_id": source_id, "status": "failed",
                                "error": type(error).__name__ + ": " + str(error)[:500]})

    candidates = []
    seen = set()
    candidate_paths = []
    for source_id in selected:
        path = media_root / source_id / "candidates.jsonl"
        if not path.exists():
            continue
        candidate_paths.append(str(path.resolve()))
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            identity = item.get("candidate_id") or item.get("source_url")
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append(item)
    combined = output / "oil-history-candidates.jsonl"
    combined.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in candidates), encoding="utf-8")
    report = {
        "start_date": start_date,
        "end_date": end_date,
        "sources": selected,
        "candidate_count": len(candidates),
        "candidate_paths": candidate_paths,
        "combined_candidates": str(combined.resolve()),
        "source_reports": sorted(reports, key=lambda value: value.get("source_id", "")),
    }
    (output / "history-crawl-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output, combined, report


def main():
    parser = argparse.ArgumentParser(description="使用原油期货信源回溯抓取黄金影响新闻")
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default=today_local())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--sources", help="逗号分隔；默认金十、财联社、同花顺、界面、证券时报、AAStocks、财新")
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--max-articles", type=int, default=50000)
    parser.add_argument("--no-build", action="store_true", help="只抓取候选，不写入黄金数据库")
    args = parser.parse_args()
    selected = [value.strip() for value in args.sources.split(",") if value.strip()] if args.sources else DEFAULT_SOURCES
    output, combined, report = collect(args.start_date, args.end_date, args.output, selected,
                                       args.max_pages, args.max_articles)
    result = {"crawl": report}
    if not args.no_build:
        result["build"] = build_gold_database(output=output / "gold-build", database=args.database,
                                               input_candidates=combined, limit_per_source=100)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
