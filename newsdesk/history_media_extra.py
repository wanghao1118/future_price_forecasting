"""Public CLS topic history supplement; collection never decides relevance.

The unsigned latest-flash cache has a different retention window from topic
archives. This adapter follows the ordinary anonymous topic page's load-more
protocol, including its public, keyless SHA1/MD5 parameter digest. It sends no
cookies, user tokens or subscriber credentials. The implementation is based on
the request and load-more functions in CLS's publicly served frontend bundles:
https://wwwjs.cls.cn/_next/static/chunks/pages/_app-ea7fc7a9f15491cb.js
https://wwwjs.cls.cn/_next/static/chunks/pages/subject/%5Bid%5D-c19f6bde89bac395.js

"Complete" covers the configured topics' descending lists within the requested
dates, not every article on CLS. All candidates still require the CLI Agent.
"""

import hashlib
import json
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode

from newsdesk.history_media import HistoryFetcher, TZ, _read_jsonl, now, publication_day
from newsdesk.ingest import write_json
from newsdesk.news_sources import candidate, parse_cls_page, text_only


DEFAULT_TOPIC_IDS = ("1583", "1101", "10986", "11394", "1515")
TOPICS = {"1583": "原油市场动态", "1101": "能源行业新闻", "10986": "中东冲突",
          "11394": "能源类期货", "1515": "石油化工",
          # IDs and names verified against each official subject page and its
          # anonymous January/September 2026 history response. They extend the
          # explicit allowlist without changing the original default scope.
          "3140": "美联储动态", "7381": "美国宏观速递", "1622": "美国经济",
          "1605": "货币政策动向", "1350": "中国央行动态", "2565": "欧洲央行动态",
          "2223": "美债动态", "1225": "经济数据及解读", "1086": "官方经济政策",
          "1556": "环球市场情报", "1741": "海外大宗商品",
          "11424": "上市公司套期保值", "11425": "期货交易所"}
PUBLIC_ACCESS_ERRORS = {"20101", "10016", "30002"}


class PublicAccessLimit(ValueError):
    """An explicit login/permission limit; never retry with credentials."""


def topic_url(topic_id, cursor):
    """Match the normal anonymous website's keyless request helper.

    Only fixed public parameters are accepted. This function cannot attach a
    token, uid, cookie, or a user-controlled signature parameter.
    """
    topic_id = str(topic_id)
    if topic_id not in TOPICS or type(cursor) is not int or cursor < 0:
        raise ValueError("未知财联社专题或无效公开时间游标")
    params = {"Subject_Id": topic_id, "last_article_time": cursor,
              "os": "web", "sv": "8.7.9", "app": "CailianpressWeb"}
    canonical = "&".join(str(key) + "=" + str(params[key]) for key in sorted(params))
    sha1 = hashlib.sha1(canonical.encode("utf-8")).hexdigest()
    params["sign"] = hashlib.md5(sha1.encode("ascii")).hexdigest()
    return "https://www.cls.cn/api/subject/" + topic_id + "/article?" + urlencode(params)


def topic_rows(raw, topic_id, raw_path, collected_at):
    payload = json.loads(raw)
    error = str(payload.get("errno"))
    if error in PUBLIC_ACCESS_ERRORS:
        raise PublicAccessLimit("财联社公开专题返回访问限制：" + str(payload.get("msg") or error)[:180])
    if error != "0" or not isinstance(payload.get("data"), list):
        raise ValueError("财联社公开专题没有成功返回列表：" + str(payload.get("msg") or error)[:180])
    result = []
    for value in payload["data"]:
        if not isinstance(value, dict) or not str(value.get("article_id", "")).isdigit():
            continue
        is_flash = value.get("article_type") == -1
        title = text_only(value.get("article_title"))
        # For a flash, article_title is the source's actual full flash text.
        # For a long article, never substitute its headline for missing text.
        body = title if is_flash else text_only(value.get("article_brief") or value.get("article_guide_text"))
        row = candidate("cls", {"id": value["article_id"], "title": title,
                                 "content": body, "ctime": value.get("article_time")},
                        raw_path, collected_at)
        if row is None:
            continue
        row.update(content_scope="public_flash" if is_flash else "public_article_excerpt",
                   discovery_topic=TOPICS[topic_id], discovery_topic_id=topic_id,
                   discovery_raw_path=str(Path(raw_path).resolve()),
                   source_listing_url="https://www.cls.cn/subject/" + topic_id,
                   collection_platform="财联社公开专题历史")
        result.append(row)
    return payload["data"], result


def enrich_article(row, fetcher):
    raw, path, collected = fetcher.get("cls", row["source_url"])
    detail = parse_cls_page(raw, detail=True)
    if str(detail.get("id")) != row["external_id"]:
        raise ValueError("财联社详情编号与发现记录不一致")
    result = dict(row)
    result["title"] = text_only(detail.get("title"))[:400] or row["title"]
    # Honor the source's own public/free marker before reading its full body.
    if detail.get("isFree") is True and text_only(detail.get("content")):
        result.update(body=text_only(detail["content"])[:20000], content_scope="public_article")
    else:
        result["body"] = (text_only(detail.get("brief")) or row["body"])[:20000]
        result["access_limit"] = "public_excerpt_only"
    result.update(raw_path=str(Path(path).resolve()), collected_at=collected)
    # Keep the topic's publication timestamp if the detail has no valid ctime.
    dated = candidate("cls", {"id": detail["id"], "title": result["title"],
                               "content": result["body"], "ctime": detail.get("ctime")}, path, collected)
    if dated and dated.get("published_at"):
        result["published_at"] = dated["published_at"]
    return result


def run_source(source_id, start_date, end_date, output_dir, *, max_pages=50, max_articles=None,
               resume=True, fetch_articles=True, fetcher=None, topic_ids=None):
    """Advance durable public topic cursors with bounded page/article work.

    Use a different output directory from the original flash-feed adapter.
    Repeated calls with the same scope continue from pending rows and topic
    cursors. Errors retry at most three times before the next topic is tried.
    """
    if source_id != "cls":
        raise ValueError("此补采适配仅支持财联社公开专题")
    start, end = date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date))
    if start > end or end > datetime.now(TZ).date():
        raise ValueError("历史日期范围无效或包含未来日期")
    if type(max_pages) is not int or max_pages < 1 or (max_articles is not None and (type(max_articles) is not int or max_articles < 1)):
        raise ValueError("max_pages/max_articles 必须是正整数")
    if isinstance(topic_ids, str):
        raise ValueError("专题范围必须为已验证 ID 的序列")
    topics = list(dict.fromkeys(str(value) for value in (DEFAULT_TOPIC_IDS if topic_ids is None else topic_ids)))
    if not topics or any(value not in TOPICS for value in topics):
        raise ValueError("未知或空财联社专题范围")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    checkpoint, destination = output / "checkpoint.json", output / "candidates.jsonl"
    scope = {"adapter": "cls_public_topics", "source_id": source_id,
             "requested_start": str(start), "requested_end": str(end), "topic_ids": topics}
    if checkpoint.exists() and resume:
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        if any(state.get(key) != value for key, value in scope.items()):
            raise ValueError("专题历史断点属于不同日期或专题范围，请使用不同输出目录")
    else:
        if destination.exists() and destination.stat().st_size:
            raise ValueError("非空专题输出目录不能无断点重新使用")
        end_cursor = int(datetime.combine(end + timedelta(days=1), day_time(), TZ).timestamp())
        state = {"version": 1, **scope, "started_at": now(), "topic_index": 0,
                 "pages": 0, "pending": [], "processed_article_count": 0, "errors": [],
                 "invalid_count": 0, "failed_attempts": {}, "observed_earliest": None,
                 "observed_latest": None, "topics": {key: {"cursor": min(end_cursor, int(datetime.now(TZ).timestamp())),
                 "pages": 0, "status": "pending", "reason": "", "fingerprints": []} for key in topics}}
        destination.touch(exist_ok=True)
        write_json(checkpoint, state)
    fetcher = fetcher or HistoryFetcher(output)
    seen = {row["candidate_id"] for row in _read_jsonl(destination)}
    invalid_path = output / "invalid-candidates.jsonl"
    invalid_ids = {value["candidate"]["candidate_id"] for value in _read_jsonl(invalid_path)}
    pages_this_call = processed_this_call = new_candidates = 0
    start_epoch = int(datetime.combine(start, day_time(), TZ).timestamp())

    def save():
        write_json(checkpoint, state)

    def finish_topic(status, reason):
        current = state["topics"][topics[state["topic_index"]]]
        current.update(status=status, reason=reason)
        state["topic_index"] += 1
        state.pop("last_error", None)

    def record(row):
        nonlocal new_candidates
        day = publication_day(row)
        if day:
            value = str(day)
            state["observed_earliest"] = min(state["observed_earliest"] or value, value)
            state["observed_latest"] = max(state["observed_latest"] or value, value)
        if row["candidate_id"] in seen or (day and not start <= day <= end):
            return
        if not day or not row.get("title", "").strip() or not row.get("body", "").strip():
            if row["candidate_id"] not in invalid_ids:
                with invalid_path.open("a", encoding="utf-8") as target:
                    target.write(json.dumps({"reason": "日期或公开正文缺失", "candidate": row}, ensure_ascii=False) + "\n")
                invalid_ids.add(row["candidate_id"])
            state["invalid_count"] = len(invalid_ids)
            return
        with destination.open("a", encoding="utf-8") as target:
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
        seen.add(row["candidate_id"])
        new_candidates += 1

    while state["pending"] or state["topic_index"] < len(topics):
        if state["pending"]:
            if max_articles is not None and processed_this_call >= max_articles:
                break
            row = state["pending"][0]
            processed_this_call += 1
            state["processed_article_count"] += 1
            day = publication_day(row)
            if row["candidate_id"] not in seen and day and start <= day <= end and row["content_scope"] != "public_flash" and fetch_articles:
                try:
                    row = enrich_article(row, fetcher)
                except Exception as error:
                    # Retain only an actual public excerpt on detail failure.
                    row["content_fetch_error"] = type(error).__name__ + ": " + str(error)[:220]
                    state["errors"].append({"url": row["source_url"], "error": row["content_fetch_error"],
                                             "public_excerpt_retained": bool(row.get("body"))})
            record(row)
            state["pending"].pop(0)
            save()
            continue
        if pages_this_call >= max_pages or (max_articles is not None and processed_this_call >= max_articles):
            break
        topic_id = topics[state["topic_index"]]
        current = state["topics"][topic_id]
        url = topic_url(topic_id, current["cursor"])
        pages_this_call += 1
        try:
            raw, path, collected = fetcher.get("cls", url)
            upstream, rows = topic_rows(raw, topic_id, path, collected)
            fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            if fingerprint in current["fingerprints"]:
                finish_topic("blocked", "专题公开游标返回重复页面，未跳过不确定区间")
                save()
                continue
            current["fingerprints"].append(fingerprint)
            current["pages"] += 1
            current["status"] = "incomplete"
            state["pages"] += 1
            state.pop("last_error", None)
            state["pending"] = rows
            # Use source timestamps, not inferred dates from titles/search text.
            stamps = []
            for value in upstream:
                try:
                    stamp = int(value["article_time"])
                    if stamp > 0:
                        stamps.append(stamp)
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
            if not upstream:
                finish_topic("blocked", "专题公开列表在越过开始日期前已无数据，无法证明完整覆盖")
            elif not stamps or min(stamps) >= current["cursor"]:
                finish_topic("blocked", "专题公开游标未向历史推进，未跳过同秒新闻")
            else:
                current["cursor"] = min(stamps)
                if current["cursor"] < start_epoch:
                    finish_topic("complete", "专题公开倒序列表已越过请求开始日期")
            save()
        except Exception as error:
            attempts = state["failed_attempts"].get(url, 0) + 1
            state["failed_attempts"][url] = attempts
            failure = {"url": url, "error": type(error).__name__ + ": " + str(error)[:300]}
            state["last_error"] = failure
            denied = isinstance(error, PublicAccessLimit) or (isinstance(error, HTTPError) and error.code in {401, 403})
            if attempts >= 3 or denied:
                state["errors"].append(failure)
                finish_topic("blocked", "官网明确限制匿名访问" if denied else "公开专题请求有界重试后失败，继续其他专题")
                save()
                continue
            save()
            break
    terminal = state["topic_index"] >= len(topics) and not state["pending"]
    all_complete = all(value["status"] == "complete" for value in state["topics"].values())
    status = "complete" if terminal and all_complete and not state["errors"] and not state["invalid_count"] else "incomplete"
    if terminal and not seen and not all_complete:
        status = "blocked"
    last_url = state.get("last_error", {}).get("url")
    remaining = max(0, 3 - state["failed_attempts"].get(last_url, 3)) if last_url and not terminal else 0
    errors = list(state["errors"])
    if state.get("last_error") and state["last_error"] not in errors:
        errors.append(state["last_error"])
    progress = [{"topic_id": key, "topic_name": TOPICS[key], **{field: value[field] for field in ("cursor", "pages", "status", "reason")}}
                for key, value in state["topics"].items()]
    result = {"source_id": "cls", "source_name": "财联社", "adapter": "cls_public_topics", "status": status,
              "topic_ids": topics,
              "terminal": terminal, "candidate_count": len(seen), "new_candidates": new_candidates,
              "pages": state["pages"], "pages_this_call": pages_this_call,
              "processed_article_count": state["processed_article_count"], "invalid_count": state["invalid_count"],
              "completed_topics": state["topic_index"], "progress": progress,
              "retry_pending": bool(remaining), "retry_attempts_remaining": remaining, "errors": errors,
              "candidates_path": str(destination), "checkpoint_path": str(checkpoint), "updated_at": now(),
              "coverage": {"requested_start": str(start), "requested_end": str(end),
                           "observed_earliest": state["observed_earliest"], "observed_latest": state["observed_latest"],
                           "candidate_count": len(seen), "complete": status == "complete", "topics": progress,
                           "topic_ids": topics,
                           "scope": "财联社所配置公开专题历史列表；不代表财联社全站，候选仍须 Agent 逐条判断",
                           "reason": "已结束所配置专题的公开回溯，见各专题覆盖状态" if terminal else "本轮工作量上限或有界重试，保留断点待续跑"}}
    write_json(output / "report.json", result)
    return result
