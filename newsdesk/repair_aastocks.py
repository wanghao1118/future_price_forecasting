"""One-off, staged AAStocks body repair with a separate ordinary Agent review.

This module never invokes a model. ``prepare`` reads the live database and
creates an isolated stage. Run the ordinary ingest reviewer on that stage,
then ``commit`` compares each original SQLite payload and update timestamp
before replacing it. Conflicting live records remain untouched.
"""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from newsdesk.ingest import (REVIEW_MODEL, canonical_url, now, prepare_candidate,
                             validate_decision, write_json)
from newsdesk.server import NewsStore, validate_agent_review


def _read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").split("\n") if line.strip()]


def _write_jsonl(path, records):
    Path(path).write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in records), encoding="utf-8")


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _index(rows, field):
    result = {}
    for row in rows:
        value = row.get(field)
        if not isinstance(value, str) or not value or value in result:
            raise ValueError("修复包有缺失或重复的 " + field)
        result[value] = row
    return result


def _readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _review_fields(review, decision):
    value = {"reviewer": review["reviewer"], "reviewed_at": review["reviewed_at"],
        "relevance": decision["relevant"], "relevance_reason": decision["reason"],
        "category_reasons": {item["category"]: item["reason"] for item in decision["category_reasons"]},
        "evidence": decision["evidence"], "confidence": decision["confidence"]}
    return validate_agent_review(value, decision["categories"])


def _decision_fields_match(record, decision):
    fields = {**decision["attributes"], "summary": decision["summary"],
              "category": decision["primary_category"], "categories": decision["categories"],
              "confidence": decision["confidence"]}
    for field, value in fields.items():
        if isinstance(value, str):
            value = value.strip()
        elif isinstance(value, list):
            value = list(dict.fromkeys(entry.strip() for entry in value))
        if record.get(field) != value:
            return False
    return True


def _auto_review_unchanged(record, candidate, audit):
    path = audit.get("cached_auto_review_path")
    if not path or not Path(path).is_file():
        return False
    cached = json.loads(Path(path).read_text(encoding="utf-8"))
    if cached.get("content_hash") != candidate["content_hash"] or cached.get("model") != REVIEW_MODEL:
        return False
    decision = validate_decision(cached.get("decision"), candidate)
    if not decision["relevant"]:
        return False
    expected_review = _review_fields(cached, decision)
    if record.get("agent_review") != expected_review:
        return False
    if not _decision_fields_match(record, decision):
        return False
    # These are user-controlled fields that an ordinary positive archive can
    # reset. Protect non-default values rather than inferring who edited them.
    return (record.get("verification") == "待核验" and not record.get("contract")
            and not record.get("is_demo") and record.get("update_note") ==
            "公开来源采集；agent 逐条判断相关性与分类。内容范围：" + candidate.get("content_scope", "公开快讯"))


def prepare(package, database, workdir):
    package, database, workdir = Path(package).resolve(), Path(database).resolve(), Path(workdir).resolve()
    if not database.is_file():
        raise ValueError("原新闻库不存在，未创建新库")
    if workdir.exists() and any(workdir.iterdir()):
        raise ValueError("修复工作目录必须为空，不能覆盖已有暂存或审计结果")
    if database == workdir / "stage.sqlite3":
        raise ValueError("原库不能作为暂存库")
    original = _index([prepare_candidate(row) for row in _read_jsonl(package / "original-candidates.jsonl")], "candidate_id")
    safe = _index([prepare_candidate(row) for row in _read_jsonl(package / "review-safe-candidates.jsonl")], "candidate_id")
    audits = _index(_read_jsonl(package / "affected-audit.jsonl"), "candidate_id")
    snapshots = _index(_read_jsonl(package / "database-before.jsonl"), "id")
    before, candidates, skipped = [], [], []
    with _readonly(database) as connection:
        connection.execute("BEGIN")
        for candidate_id, clean in safe.items():
            try:
                old, audit = original[candidate_id], audits[candidate_id]
                news_id = audit.get("news_id")
                snapshot = snapshots[news_id]
                if audit.get("safe_for_root_review") is not True:
                    raise ValueError("原修复包未将此条标为安全复核")
                if clean["source_id"] != "aastocks" or old["source_id"] != "aastocks":
                    raise ValueError("修复包包含其他来源")
                if (old["content_hash"] != audit.get("old_content_hash")
                        or clean["content_hash"] != audit.get("new_content_hash")
                        or old["content_hash"] == clean["content_hash"]
                        or old["body"] != audit.get("old_body") or clean["body"] != audit.get("clean_body")):
                    raise ValueError("修复包的新旧正文或哈希不一致")
                for field in ("title", "source_url", "published_at", "published_date", "raw_path", "external_id"):
                    if old.get(field, "") != clean.get(field, ""):
                        raise ValueError("正文修复改变了来源身份或日期：" + field)
                if not Path(old.get("raw_path", "")).is_file():
                    raise ValueError("缺少原始来源证据")
                current = connection.execute("SELECT payload,updated_at FROM news WHERE id=?", (news_id,)).fetchone()
                if current is None:
                    raise ValueError("原记录已删除，禁止新建")
                record = json.loads(current[0])
                if record != snapshot:
                    raise ValueError("完整记录已在原快照后变化，保护当前记录")
                metadata = record.get("ingestion", {})
                if (metadata.get("source_id") != "aastocks" or metadata.get("external_id") != old.get("external_id")
                        or metadata.get("content_hash") != old["content_hash"]
                        or metadata.get("raw_path") != old.get("raw_path")
                        or canonical_url(record["source_url"]) != old["canonical_url"]
                        or record.get("title") != old["title"] or record.get("body") != old["body"]
                        or record.get("published_at", "") != old.get("published_at", "")
                        or metadata.get("published_date", "") != old.get("published_date", "")):
                    raise ValueError("当前原文、日期、来源或旧哈希未精确匹配")
                if not _auto_review_unchanged(record, old, audit):
                    raise ValueError("自动审核或人工研究字段无法重核，保护当前记录")
                before.append({"news_id": news_id, "candidate_id": candidate_id,
                               "payload": current[0], "table_updated_at": current[1],
                               "old_content_hash": old["content_hash"], "new_content_hash": clean["content_hash"]})
                candidates.append(clean)
            except (KeyError, ValueError, TypeError, OSError) as error:
                skipped.append({"candidate_id": candidate_id, "news_id": audits.get(candidate_id, {}).get("news_id"),
                                "reason": str(error)})
    if len({row["news_id"] for row in before}) != len(before):
        raise ValueError("多条候选对应同一新闻，拒绝有歧义的修复包")
    workdir.mkdir(parents=True, exist_ok=True)
    stage, input_path, before_path = workdir / "stage.sqlite3", workdir / "stage-input.jsonl", workdir / "before.jsonl"
    _write_jsonl(before_path, before)
    _write_jsonl(input_path, candidates)
    _write_jsonl(workdir / "prepare-skipped.jsonl", skipped)
    store = NewsStore(stage, seed_demo=False)
    by_candidate = {row["candidate_id"]: row for row in candidates}
    with store.connect() as connection:
        for row in before:
            record = json.loads(row["payload"])
            clean = by_candidate[row["candidate_id"]]
            record["body"] = clean["body"]
            record["ingestion"]["content_hash"] = clean["content_hash"]
            # Preserve the old ID and every other field. The existing negative
            # archive branch now sees the clean hash and can exclude this ID.
            NewsStore.write(connection, record)
    manifest = {"version": 1, "prepared_at": now(), "database": str(database), "package": str(package),
                "workdir": str(workdir), "stage_database": str(stage), "stage_input": str(input_path),
                "before_path": str(before_path), "before_sha256": _digest(before_path),
                "input_sha256": _digest(input_path), "prepared_count": len(before), "skipped_count": len(skipped),
                "pipeline": str(workdir / "pipeline.json"), "decisions": str(workdir / "decisions.json"),
                "status": "prepared", "model": REVIEW_MODEL}
    write_json(workdir / "prepare-manifest.json", manifest)
    return manifest


def _verified_stage(manifest):
    before_path, input_path = Path(manifest["before_path"]), Path(manifest["stage_input"])
    if _digest(before_path) != manifest["before_sha256"] or _digest(input_path) != manifest["input_sha256"]:
        raise ValueError("暂存输入或原始记录备份在 prepare 后被改变")
    before = _read_jsonl(before_path)
    candidates = _index([prepare_candidate(row) for row in _read_jsonl(input_path)], "candidate_id")
    expected = _index(before, "news_id")
    if len(expected) != manifest["prepared_count"] or len(candidates) != len(expected):
        raise ValueError("暂存条目数量不一致")
    pipeline_path = Path(manifest["pipeline"])
    pipeline = json.loads(pipeline_path.read_text(encoding="utf-8")) if pipeline_path.exists() else {}
    if (pipeline.get("status") != "complete" or pipeline.get("model") != REVIEW_MODEL
            or pipeline.get("candidate_count") != len(candidates) or pipeline.get("reviewed_count") != len(candidates)
            or pipeline.get("created") != 0):
        raise ValueError("暂存 Agent 审核尚未完整成功，禁止提交")
    decisions = json.loads(Path(manifest["decisions"]).read_text(encoding="utf-8"))
    if not isinstance(decisions, list) or len(decisions) != len(candidates):
        raise ValueError("缺少完整的逐条 Agent 审核记录")
    reviews = _index([{**review, "candidate_id": review.get("decision", {}).get("candidate_id")}
                      for review in decisions], "candidate_id")
    if set(reviews) != set(candidates):
        raise ValueError("Agent 审核 ID 与暂存输入不同")
    with _readonly(manifest["stage_database"]) as connection:
        staged = {row[0]: (row[1], row[2]) for row in connection.execute("SELECT id,payload,updated_at FROM news")}
    if set(staged) != set(expected):
        raise ValueError("暂存库有新增或缺失 ID，禁止提交")
    for news_id, saved in expected.items():
        candidate = candidates[saved["candidate_id"]]
        review = reviews[saved["candidate_id"]]
        decision = validate_decision(review["decision"], candidate)
        record = json.loads(staged[news_id][0])
        metadata = record.get("ingestion", {})
        if (record.get("id") != news_id or metadata.get("source_id") != "aastocks"
                or metadata.get("external_id") != candidate.get("external_id")
                or metadata.get("content_hash") != saved["new_content_hash"]
                or metadata.get("content_hash") != candidate["content_hash"]
                or record.get("body") != candidate["body"] or record.get("title") != candidate["title"]
                or record.get("published_at", "") != candidate.get("published_at", "")
                or canonical_url(record["source_url"]) != candidate["canonical_url"]
                or review.get("content_hash") != candidate["content_hash"] or review.get("model") != REVIEW_MODEL
                or record.get("agent_review") != _review_fields(review, decision)
                or (decision["relevant"] and not _decision_fields_match(record, decision))
                or record.get("created_at") != json.loads(saved["payload"]).get("created_at")):
            raise ValueError("暂存记录未完整对应清理后正文与新审核：" + news_id)
    return before, staged


def commit(workdir, database=None):
    workdir = Path(workdir).resolve()
    manifest = json.loads((workdir / "prepare-manifest.json").read_text(encoding="utf-8"))
    target = Path(database).resolve() if database is not None else Path(manifest["database"])
    if target != Path(manifest["database"]) or target == Path(manifest["stage_database"]):
        raise ValueError("提交目标与 prepare 的原库不同")
    before, staged = _verified_stage(manifest)
    audit_path, report_path = workdir / "commit-audit.jsonl", workdir / "commit-report.json"
    if report_path.exists():
        previous = json.loads(report_path.read_text(encoding="utf-8"))
        if (previous.get("status") == "complete" and previous.get("database") == str(target)
                and previous.get("prepared_count") == len(before) and audit_path.is_file()):
            return previous
        raise ValueError("已有无法核验的提交结果，未重复提交或覆盖原审计")
    report = {"started_at": now(), "database": str(target), "prepared_count": len(before),
              "updated": 0, "excluded": 0, "skipped": 0, "created": 0, "status": "complete"}
    audit = []
    # Persist intended before/after images before opening a write transaction.
    # They remain recoverable even if the process exits after SQLite commits
    # but before the final outcome report can be written.
    _write_jsonl(workdir / "commit-plan.jsonl", [
        {**row, "after_payload": staged[row["news_id"]][0],
         "after_table_updated_at": staged[row["news_id"]][1]} for row in before])
    with sqlite3.connect(target.resolve().as_uri() + "?mode=rw", uri=True, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        for original in before:
            news_id = original["news_id"]
            current = connection.execute("SELECT payload,updated_at FROM news WHERE id=?", (news_id,)).fetchone()
            after_payload, after_updated_at = staged[news_id]
            event = {"news_id": news_id, "candidate_id": original["candidate_id"],
                     "before_payload": original["payload"], "before_table_updated_at": original["table_updated_at"],
                     "after_payload": after_payload, "after_table_updated_at": after_updated_at}
            if current is None or current != (original["payload"], original["table_updated_at"]):
                report["skipped"] += 1
                event.update(action="skipped", reason="原记录删除或在 prepare 后变化，未覆盖当前记录")
            else:
                changed = connection.execute("UPDATE news SET payload=?,updated_at=? WHERE id=? AND payload=? AND updated_at=?",
                    (after_payload, after_updated_at, news_id, original["payload"], original["table_updated_at"])).rowcount
                if changed != 1:
                    raise RuntimeError("事务内比较交换未更新唯一原记录")
                record = json.loads(after_payload)
                action = "excluded" if record["agent_review"]["relevance"] is False else "updated"
                report[action] += 1
                event["action"] = action
            audit.append(event)
    report.update(completed_at=now(), audit_path=str(audit_path), skipped_ids=[row["news_id"] for row in audit if row["action"] == "skipped"])
    _write_jsonl(audit_path, audit)
    write_json(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    first = subcommands.add_parser("prepare", help="只读原库并建立独立暂存与审核输入")
    first.add_argument("--package", type=Path, required=True)
    first.add_argument("--database", type=Path, required=True)
    first.add_argument("--workdir", type=Path, required=True)
    last = subcommands.add_parser("commit", help="Agent 暂存审核完整成功后，CAS 提交未变化记录")
    last.add_argument("--workdir", type=Path, required=True)
    last.add_argument("--database", type=Path)
    args = parser.parse_args()
    result = prepare(args.package, args.database, args.workdir) if args.command == "prepare" else commit(args.workdir, args.database)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
