"""Fair public CLS macro/financial discovery alongside the oil-topic archive.

Each verified topic owns independent monthly cursors. Topic and month rotation
only schedules collection: every public item still needs the ordinary semantic
Agent before it may enter the oil-news archive. This module never writes a DB.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from newsdesk import history_priority_topics as monthly
from newsdesk.history_media import HistoryFetcher, TZ, now, publication_day
from newsdesk.history_merge import merge_channel_reports
from newsdesk.ingest import write_json


MACRO_TOPICS = {
    "3140": "美联储动态", "7381": "美国宏观速递", "1622": "美国经济",
    "1605": "货币政策动向", "1350": "中国央行动态", "2565": "欧洲央行动态",
    "2223": "美债动态", "1225": "经济数据及解读", "1086": "官方经济政策",
    "1556": "环球市场情报", "1741": "海外大宗商品",
    "11424": "上市公司套期保值", "11425": "期货交易所",
}
MACRO_TOPIC_IDS = tuple(MACRO_TOPICS)


def _months(first, last):
    current, result = first.replace(day=1), []
    while current <= last:
        result.append(current.strftime("%Y-%m"))
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return result


def _terminal(report):
    return (report.get("terminal") is True or report.get("coverage", {}).get("terminal") is True
            or report.get("status") in {"complete", "blocked", "failed"}
            or report.get("coverage", {}).get("complete") is True)


def _priority_finished(report, priority):
    channels = report.get("channels", {})
    return all(_terminal(channels.get(month, {})) for month in priority)


def _scope_matches(report, topic, start, end):
    coverage = report.get("coverage", {})
    topic_ids = report.get("topic_ids", coverage.get("topic_ids"))
    return (report.get("source_id") == "cls" and coverage.get("requested_start") == start
            and coverage.get("requested_end") == end and topic_ids == [topic])


def _distribution(path, months):
    counts, seen = {month: 0 for month in months}, set()
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            item = json.loads(line)
            identity = item.get("candidate_id") or item.get("source_url")
            if identity in seen:
                continue
            seen.add(identity)
            day = publication_day(item)
            if day and day.isoformat()[:7] in counts:
                counts[day.isoformat()[:7]] += 1
    return counts


def run_source(source_id, start_date, end_date, output_dir, *, priority_months=None,
               max_pages=18, max_articles=180, fetcher=None, topic_ids=None):
    """Advance disjoint topic/month checkpoints within a shared call budget.

    A topic receives at most two pages per call and an equal share of remaining
    article work. Different topics begin with different priority months; saved
    lower-level month cursors then rotate normally. No topic may advance its
    nonpriority months while another topic still has priority-month work.
    """
    if source_id != "cls":
        raise ValueError("宏观金融专题补采仅支持财联社公开来源")
    first, last = date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date))
    if first > last or last > datetime.now(TZ).date():
        raise ValueError("历史日期范围无效或包含未来日期")
    if (type(max_pages) is not int or max_pages < 1
            or max_articles is not None and (type(max_articles) is not int or max_articles < 1)):
        raise ValueError("max_pages/max_articles 必须是正整数")
    if isinstance(topic_ids, str) or isinstance(priority_months, str):
        raise ValueError("专题和优先月份必须为已验证范围的序列")
    selected = list(dict.fromkeys(str(value) for value in (
        MACRO_TOPIC_IDS if topic_ids is None else topic_ids)))
    if not selected or any(topic not in MACRO_TOPICS for topic in selected):
        raise ValueError("未知或空宏观金融专题范围")
    months = _months(first, last)
    priority = months if priority_months is None else list(dict.fromkeys(priority_months))
    if any(not isinstance(month, str) or month not in months for month in priority):
        raise ValueError("优先月份必须属于请求日期范围，格式为 YYYY-MM")
    start, end = str(first), str(last)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    checkpoint = output / "macro-checkpoint.json"
    scope = {"adapter": "cls_macro_financial_topics", "source_id": source_id,
             "requested_start": start, "requested_end": end, "topic_ids": selected}
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        if any(state.get(key) != value for key, value in scope.items()):
            raise ValueError("宏观金融断点属于不同来源、日期或专题范围")
    else:
        # A prebuilt fetcher legitimately creates raw/. Conversely, an existing
        # five-topic monthly/channel directory must never be repurposed here.
        legacy_files = ("monthly-checkpoint.json", "checkpoint.json", "candidates.jsonl",
                        "combined-report.json", "combined-candidates.jsonl")
        if any((output / name).exists() for name in legacy_files):
            raise ValueError("输出目录已有其他历史通道数据，请使用独立宏观金融目录")
        previous_report = output / "report.json"
        if previous_report.exists():
            saved = json.loads(previous_report.read_text(encoding="utf-8"))
            coverage = saved.get("coverage", {})
            if (saved.get("adapter") != scope["adapter"] or saved.get("source_id") != source_id
                    or saved.get("topic_ids") != selected or coverage.get("requested_start") != start
                    or coverage.get("requested_end") != end):
                raise ValueError("输出目录的旧报告不属于本宏观金融范围")
        state = {"version": 1, **scope, "started_at": now(), "next_topic": None,
                 "topics": {}, "candidate_count": 0}
    if (not isinstance(state.get("topics"), dict)
            or any(topic not in selected for topic in state["topics"])):
        raise ValueError("宏观金融断点包含不属于所请求范围的子专题")
    for topic, report in state["topics"].items():
        if not isinstance(report, dict) or not _scope_matches(report, topic, start, end):
            raise ValueError("已存子专题状态的来源、日期或单专题范围不一致")
    for topic in selected:
        child_report = output / "topics" / topic / "report.json"
        if child_report.exists():
            report = json.loads(child_report.read_text(encoding="utf-8"))
            if not _scope_matches(report, topic, start, end):
                raise ValueError("子专题报告的来源、日期或单专题范围不一致")
            state["topics"][topic] = report
        state["topics"].setdefault(topic, {"source_id": "cls", "status": "pending", "terminal": False,
            "topic_ids": [topic], "candidate_count": 0, "pages": 0, "processed_article_count": 0,
            "coverage": {"requested_start": start, "requested_end": end, "topic_ids": [topic],
                         "complete": False, "scope": "尚未开始该公开宏观金融专题"}})
    state["priority_months"] = priority
    before_count = state.get("candidate_count", 0)
    active_priority = [topic for topic in selected
                       if not _terminal(state["topics"][topic])
                       and not _priority_finished(state["topics"][topic], priority)]
    eligible = active_priority or [topic for topic in selected if not _terminal(state["topics"][topic])]
    if state.get("next_topic") in selected:
        offset = selected.index(state["next_topic"])
        ordered = selected[offset:] + selected[:offset]
        eligible = [topic for topic in ordered if topic in eligible]
    slots = min(len(eligible), (max_pages + 1) // 2,
                max_articles if max_articles is not None else len(eligible))
    planned = eligible[:slots]
    used_pages = used_articles = 0
    visited = []
    fetcher = fetcher or HistoryFetcher(output)
    for index, topic in enumerate(planned):
        pages_left = max_pages - used_pages
        articles_left = None if max_articles is None else max_articles - used_articles
        if pages_left <= 0 or articles_left is not None and articles_left <= 0:
            break
        turns_left = len(planned) - index
        share = None if articles_left is None else max(1, articles_left // turns_left)
        offset = selected.index(topic) % len(priority) if priority else 0
        shifted_priority = priority[offset:] + priority[:offset]
        before = state["topics"][topic]
        report = monthly.run_monthly_topics(source_id, start, end, output / "topics" / topic,
            priority_months=shifted_priority, max_pages=min(2, pages_left), max_articles=share,
            fetcher=fetcher, topic_ids=[topic])
        if not _scope_matches(report, topic, start, end):
            raise ValueError("公开专题返回的来源、日期或单专题范围不一致")
        state["topics"][topic] = report
        used_pages += int(report.get("pages_this_call", 0))
        used_articles += max(0, int(report.get("processed_article_count", 0))
                             - int(before.get("processed_article_count", 0)))
        visited.append(topic)
        state["next_topic"] = selected[(selected.index(topic) + 1) % len(selected)]
        state["updated_at"] = now()
        write_json(checkpoint, state)
    merged = merge_channel_reports(source_id, start, end, output / "combined", state["topics"])
    state["candidate_count"] = merged["candidate_count"]
    state["updated_at"] = now()
    write_json(checkpoint, state)
    merged.update(adapter="cls_macro_financial_topics", source_name="财联社", topic_ids=selected,
        priority_months=priority, topics_visited_this_call=visited,
        pages=sum(int(report.get("pages", 0)) for report in state["topics"].values()),
        processed_article_count=sum(int(report.get("processed_article_count", 0))
                                    for report in state["topics"].values()),
        pages_this_call=used_pages, processed_articles_this_call=used_articles,
        new_candidates=max(0, merged["candidate_count"] - before_count),
        priority_months_terminal=all(_priority_finished(state["topics"][topic], priority) for topic in selected),
        monthly_candidate_counts=_distribution(merged["candidates_path"], months),
        checkpoint_path=str(checkpoint), updated_at=now())
    merged["coverage"].update(topic_ids=selected, topic_names={key: MACRO_TOPICS[key] for key in selected},
        months=months, priority_months=priority,
        scope="已核验的财联社宏观、金融政策及衍生品公开专题，逐专题逐月回溯；包含非原油内容，仍须 Agent 逐条判断",
        reason="所有专题均已到达各自公开边界，见下层月份状态" if merged["terminal"]
               else "专题和月份公平轮转；有限预算仅结束本轮，未处理范围保持待续跑")
    write_json(output / "report.json", merged)
    return merged
