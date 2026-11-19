"""Fair monthly CLS history discovery through its existing public adapter.

Each month has an independent date scope and public cursor.  Monthly work caps
only yield a turn; a month or topic is never called complete because its cap was
reached.  Collection and semantic oil-news classification remain separate.
"""

import json
from datetime import date, timedelta
from pathlib import Path

from newsdesk import history_media_extra as topics
from newsdesk.history_merge import merge_channel_reports
from newsdesk.ingest import write_json

HistoryFetcher = topics.HistoryFetcher


def _month_ranges(start, end):
    current = start.replace(day=1)
    result = {}
    while current <= end:
        following = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
        result[current.strftime("%Y-%m")] = (str(max(start, current)), str(min(end, following - timedelta(days=1))))
        current = following
    return result


def _terminal(report):
    return report.get("terminal") is True or report.get("status") in {"complete", "blocked", "failed"}


def run_monthly_topics(source_id, start_date, end_date, output_dir, priority_months=None, *,
                       max_pages=10, max_articles=40, fetcher=None, topic_ids=None):
    """Advance several independent month scopes, prioritizing deficient months.

    ``max_pages`` and ``max_articles`` are total per-call work budgets.  A month
    receives at most two pages in a turn and article work is shared among the
    planned month turns.  The saved next-month cursor survives process restarts.
    Priority months rotate fairly until all reach a real public terminal state;
    remaining requested months are then eligible.  All requested months remain
    in the combined coverage report, including months not yet attempted.
    """
    if source_id != "cls":
        raise ValueError("按月专题补采仅支持财联社公开专题")
    start, end = date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date))
    if start > end or end > topics.datetime.now(topics.TZ).date():
        raise ValueError("历史日期范围无效或包含未来日期")
    if type(max_pages) is not int or max_pages < 1 or (max_articles is not None and (type(max_articles) is not int or max_articles < 1)):
        raise ValueError("max_pages/max_articles 必须是正整数")
    if isinstance(topic_ids,str):
        raise ValueError("专题范围必须为已验证 ID 的序列")
    selected_topics = list(dict.fromkeys(str(value) for value in (
        topics.DEFAULT_TOPIC_IDS if topic_ids is None else topic_ids)))
    if not selected_topics or any(value not in topics.TOPICS for value in selected_topics):
        raise ValueError("未知或空财联社专题范围")
    ranges = _month_ranges(start, end)
    priority = list(ranges) if priority_months is None else list(dict.fromkeys(priority_months))
    if any(not isinstance(month, str) or month not in ranges for month in priority):
        raise ValueError("优先月份必须位于所请求日期范围内，格式为 YYYY-MM")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    checkpoint = output / "monthly-checkpoint.json"
    scope = {"source_id": source_id, "requested_start": str(start), "requested_end": str(end),
             "topic_ids": selected_topics}
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        if 'topic_ids' not in state:
            # The former wrapper could only request the original five topics.
            # Permit that one migration, and still verify any durable child
            # checkpoint so a custom topic scope cannot be silently reused.
            if selected_topics != list(topics.DEFAULT_TOPIC_IDS):
                raise ValueError("旧按月断点仅能按原五个专题迁移，请另用输出目录")
            for child in (output/'months').glob('*/checkpoint.json'):
                saved_child = json.loads(child.read_text(encoding='utf-8'))
                if saved_child.get('topic_ids') != selected_topics:
                    raise ValueError("旧按月断点的子专题范围不一致")
            state['topic_ids'] = selected_topics
        if any(state.get(key) != value for key, value in scope.items()):
            raise ValueError("按月专题断点属于不同来源、日期或专题范围")
    else:
        state = {"version": 1, **scope, "next_month": None, "months": {}, "started_at": topics.now()}
    for month, (first, last) in ranges.items():
        report_path = output / "months" / month / "report.json"
        # A stopped subprocess may have saved a month report before its parent
        # index. Recover that durable result without changing the month scope.
        if report_path.exists():
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            coverage = saved.get("coverage", {})
            if saved.get("source_id") != source_id or coverage.get("requested_start") != first or coverage.get("requested_end") != last:
                raise ValueError("月份专题报告的来源或日期范围不一致")
            saved_topics = saved.get('topic_ids') or coverage.get('topic_ids')
            child = report_path.with_name('checkpoint.json')
            if child.exists():
                child_topics = json.loads(child.read_text(encoding='utf-8')).get('topic_ids')
                if child_topics != selected_topics:
                    raise ValueError("月份子断点的专题范围不一致")
                saved_topics = child_topics
            if (saved_topics is None and selected_topics != list(topics.DEFAULT_TOPIC_IDS)
                    or saved_topics is not None and list(saved_topics) != selected_topics):
                raise ValueError("月份报告的专题范围不一致")
            state["months"][month] = saved
        state["months"].setdefault(month, {"source_id": source_id, "status": "pending", "terminal": False,
            "candidate_count": 0, "pages": 0, "processed_article_count": 0,
            "coverage": {"requested_start": first, "requested_end": last, "complete": False,
                         "scope": "尚未开始该月公开专题回溯"}})
    state["priority_months"] = priority
    active_priority = [month for month in priority if not _terminal(state["months"][month])]
    eligible = active_priority or [month for month in ranges if not _terminal(state["months"][month])]
    if eligible and state.get("next_month") in eligible:
        offset = eligible.index(state["next_month"])
        eligible = eligible[offset:] + eligible[:offset]
    slots = min(len(eligible), (max_pages + 1) // 2,
                max_articles if max_articles is not None else len(eligible))
    planned = eligible[:slots]
    used_pages = used_articles = new_candidates = 0
    visited = []
    fetcher = fetcher or topics.HistoryFetcher(output)
    for index, month in enumerate(planned):
        pages_left = max_pages - used_pages
        articles_left = None if max_articles is None else max_articles - used_articles
        if pages_left <= 0 or (articles_left is not None and articles_left <= 0):
            break
        turns_left = len(planned) - index
        article_share = None if articles_left is None else max(1, articles_left // turns_left)
        before = state["months"][month]
        first, last = ranges[month]
        optional = {'topic_ids':selected_topics} if topic_ids is not None else {}
        report = topics.run_source(source_id, first, last, output / "months" / month,
            max_pages=min(2, pages_left), max_articles=article_share, fetcher=fetcher, **optional)
        state["months"][month] = report
        visited.append(month)
        used_pages += int(report.get("pages_this_call", 0))
        used_articles += max(0, int(report.get("processed_article_count", 0)) - int(before.get("processed_article_count", 0)))
        new_candidates += int(report.get("new_candidates", 0))
        state["next_month"] = eligible[(eligible.index(month) + 1) % len(eligible)]
        state["updated_at"] = topics.now()
        write_json(checkpoint, state)
    # Include unvisited months so public completion cannot be inferred from a
    # single successful month, a finite budget, or the priority subset alone.
    merged = merge_channel_reports(source_id, str(start), str(end), output / "combined", state["months"])
    merged.update(adapter="cls_monthly_public_topics", source_name="财联社",
        pages=sum(int(report.get("pages", 0)) for report in state["months"].values()),
        processed_article_count=sum(int(report.get("processed_article_count", 0)) for report in state["months"].values()),
        pages_this_call=used_pages, processed_articles_this_call=used_articles, new_candidates=new_candidates,
        topic_ids=selected_topics,
        priority_months=priority, months_visited_this_call=visited,
        monthly_candidate_counts={month: int(report.get("candidate_count", 0)) for month, report in state["months"].items()},
        checkpoint_path=str(checkpoint), updated_at=topics.now())
    merged["coverage"].update(months=list(ranges), priority_months=priority,topic_ids=selected_topics,
        scope="所请求月份及所配置财联社公开专题；逐月独立回溯，不代表财联社全站，候选仍须 Agent 逐条判断",
        reason="全部月份已到达各自公开边界，见每月专题覆盖状态" if merged["terminal"] else "按月公平轮转，工作量上限仅结束本轮，未处理月份仍待续跑")
    write_json(checkpoint, state)
    write_json(output / "report.json", merged)
    return merged
