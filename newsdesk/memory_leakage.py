"""Frozen, source-backed historical recall questions; independent of price QA.

Editorial selections are checked against a read-only snapshot of the existing
news database at build time. HTTP reads never select new questions or call a
model. Public citations and answer evidence belong to the review dataset and
must not be included in a model's closed-book input.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from newsdesk.publication_time import require_verified_publication


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
DATASET_PATH = FIXTURES / "memory-leakage-2026.json"
SELECTION_PATHS = [FIXTURES / "memory-leakage-2026-h1.json", FIXTURES / "memory-leakage-2026-h2.json"]
VERSION = "memory-leakage-cloze-v2"
DATASET_PREFIX = "memory-leakage-2026-v2-"
TEMPORAL_FOCUSES = {"data_release", "policy_decision", "event_detail", "forecast_update"}
YEAR = 2026
MONTHS = ["{}-{:02d}".format(YEAR, month) for month in range(1, 11)]
METHODOLOGY = {
    "selection": "从现有正式新闻库逐月编辑遴选重大事件，每月10个不同事件，每个事件选1篇新闻、编写1道单空事实题；兼顾宏观政策、供需数据、地缘风险与能源贸易。重大程度为编辑判断，并非全市场事件的客观排名。",
    "temporal_specificity": "填空必须考查该次报道新公布的当期数据、决策内容、调整幅度、金额、规模、期限或事件进展。逐题复核答案与具体报道的联系；排除仅靠长期人物身份、地理常识、定义或题干计算即可作答的题。题干带日期本身不算具有时效性。",
    "month_basis": "按引用新闻的已核验首发时间归属月份，统一使用UTC+0；数据所属期、事件生效日与新闻首发日分别保留在题干中。",
    "evaluation": "闭卷作答，只提供题号与题干，不提供答案、别名、事件标题、原文证据或新闻引用；关闭联网、检索、工具与外部记忆。建议逐题独立会话，报告总正确率及分月正确率，并记录模型版本和评估日期。数字答案保留题干单位，使用参考答案或语义等价的规范化写法核对。",
    "limitation": "本题库诊断历史事实记忆。答对可能来自训练记忆、推断或猜测，不能单独证明训练数据污染或记忆泄漏；已展示题目与答案后的模型会话不可用于闭卷测试。本库以现有原油及相关宏观金融新闻为范围，不代表全部金融市场。",
}


def digest(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def text_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def valid_url(value):
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username and not parsed.password


def validate_questions(questions):
    if not isinstance(questions, list) or len(questions) != 100:
        raise ValueError("记忆泄漏题库必须恰好100题")
    counts = Counter(item.get("month") for item in questions)
    if counts != Counter({month: 10 for month in MONTHS}):
        raise ValueError("2026年1—10月必须各有10题")
    seen = {key: set() for key in ("case_id", "question", "news_id", "url", "event")}
    for item in questions:
        for key in ("case_id", "month", "event", "event_rationale", "question", "answer", "evidence", "temporal_focus", "temporal_rationale"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError("题目缺少字段：{}".format(key))
        if item["question"].count("____") != 1 or item["answer"] not in item["evidence"]:
            raise ValueError("题目必须只有一个填空，且答案必须在原文证据中：{}".format(item["case_id"]))
        if item["answer"] in item["question"]:
            raise ValueError("参考答案不能出现在题干中：{}".format(item["case_id"]))
        if item["temporal_focus"] not in TEMPORAL_FOCUSES:
            raise ValueError("题目必须标注当期数据、当次决策、事件进展或预测调整的考点")
        if not isinstance(item.get("answer_aliases"), list) or any(not isinstance(x, str) or not x.strip() for x in item["answer_aliases"]):
            raise ValueError("答案别名必须是非空字符串列表")
        if any(alias in item["question"] for alias in item["answer_aliases"]):
            raise ValueError("可接受答案不能出现在题干中：{}".format(item["case_id"]))
        source = item.get("source") or {}
        if not all(source.get(key) for key in ("news_id", "title", "source_name", "published_at", "url", "body_sha256", "publication_raw_sha256")):
            raise ValueError("题目缺少新闻引用：{}".format(item["case_id"]))
        if not valid_url(source["url"]):
            raise ValueError("新闻链接必须为HTTP(S)原文链接")
        point = datetime.fromisoformat(source["published_at"].replace("Z", "+00:00"))
        if point.tzinfo is None or point.astimezone(timezone.utc).strftime("%Y-%m") != item["month"]:
            raise ValueError("引用新闻的UTC首发月份与题目月份不一致")
        if not re.fullmatch(r"[a-f0-9]{64}", source["body_sha256"]) or not re.fullmatch(r"[a-f0-9]{64}", source["publication_raw_sha256"]):
            raise ValueError("引用新闻缺少有效的原文指纹")
        values = {"case_id": item["case_id"], "question": item["question"], "news_id": source["news_id"],
                  "url": source["url"], "event": (item["month"], re.sub(r"\s+", "", item["event"]))}
        for key, value in values.items():
            if value in seen[key]:
                raise ValueError("重复的{}：{}".format(key, value))
            seen[key].add(value)


def build_dataset(database, selection_paths=SELECTION_PATHS):
    selections = []
    for path in selection_paths:
        selections.extend(json.loads(Path(path).read_text(encoding="utf-8")))
    # One SQLite read transaction supplies a consistent source snapshot, even
    # while the independent ingestion workers continue adding news.
    connection = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT id, payload FROM news").fetchall()
    finally:
        connection.close()
    news = {identifier: json.loads(payload) for identifier, payload in rows}
    questions, source_counts = [], Counter()
    selections.sort(key=lambda item: (item["month"], news.get(item["news_id"], {}).get("published_at", ""), item["news_id"]))
    month_positions = Counter()
    for selected in selections:
        record = news.get(selected["news_id"])
        if not record or record.get("is_demo") or (record.get("agent_review") or {}).get("relevance") is not True:
            raise ValueError("引用必须来自现有正式且审核相关的新闻：{}".format(selected["news_id"]))
        published = require_verified_publication(record)
        body = record.get("body") or ""
        if not selected.get("evidence") or selected["evidence"] not in body:
            raise ValueError("证据不是新闻正文的连续原文：{}".format(selected["news_id"]))
        if selected.get("answer") not in selected["evidence"]:
            raise ValueError("答案不在新闻证据内：{}".format(selected["news_id"]))
        month_positions[selected["month"]] += 1
        meta = record["ingestion"]
        question = {key: selected[key] for key in ("month", "event", "event_rationale", "question", "answer", "answer_aliases", "evidence", "temporal_focus", "temporal_rationale")}
        question["case_id"] = "memory-{}-{:02d}".format(selected["month"], month_positions[selected["month"]])
        question["source"] = {
            "news_id": record["id"], "title": record["title"], "source_name": record["source_name"],
            "published_at": published, "url": record["source_url"], "body_sha256": text_hash(body),
            "publication_raw_sha256": meta["publication_raw_sha256"],
            "content_scope": meta.get("content_scope") or "archived_public_content",
            "verification": record.get("verification", "待核验"),
            "publication_time_verified": True,
        }
        source_counts[record["source_name"]] += 1
        questions.append(question)
    validate_questions(questions)
    payload = {
        "version": VERSION, "name": "记忆泄漏", "year": YEAR, "total_questions": len(questions),
        "months": [{"month": month, "question_count": 10} for month in MONTHS],
        "methodology": METHODOLOGY, "questions": questions,
        "source_snapshot": {"population": "existing_reviewed_relevant_non_demo_news", "utc_month_counts": dict(month_positions),
                            "source_counts": dict(source_counts), "unique_news": len({q["source"]["news_id"] for q in questions})},
    }
    fingerprint = digest(payload)
    return {**payload, "dataset_id": DATASET_PREFIX + fingerprint[:12], "content_sha256": fingerprint,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


@lru_cache(maxsize=4)
def load_dataset(path=DATASET_PATH):
    dataset = json.loads(Path(path).read_text(encoding="utf-8"))
    payload = {key: value for key, value in dataset.items() if key not in {"dataset_id", "content_sha256", "generated_at"}}
    fingerprint = digest(payload)
    if dataset.get("version") != VERSION or dataset.get("content_sha256") != fingerprint or dataset.get("dataset_id") != DATASET_PREFIX + fingerprint[:12]:
        raise ValueError("记忆泄漏题库指纹校验失败")
    validate_questions(dataset.get("questions"))
    return dataset


def main():
    parser = argparse.ArgumentParser(description="构建或核验记忆泄漏题库，不调用模型、不修改新闻库")
    parser.add_argument("action", choices=["build", "validate"])
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "news.sqlite3")
    parser.add_argument("--output", type=Path, default=DATASET_PATH)
    args = parser.parse_args()
    if args.action == "build":
        dataset = build_dataset(args.database)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        dataset = load_dataset(args.output)
    print(json.dumps({key: dataset[key] for key in ("dataset_id", "total_questions", "months", "content_sha256")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
