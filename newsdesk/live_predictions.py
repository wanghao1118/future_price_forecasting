"""Read-only, integrity-bound model predictions for one logical live question."""

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import re

from newsdesk.benchmark import DATASET_ID, LIVE_SUBMISSION_POLICY, live_submission_deadline
from newsdesk.evaluation import (AIHUB_MODELS, ACTIVE_EVALUATION_PROTOCOLS, PUSH_NEWS_PROTOCOL,
                                _validate_live_open, digest, evaluate_forecast, extract_forecast,
                                read_json, validate_run_retrieval_policy)
from newsdesk.live_prediction_batch import LIVE_PREDICTION_MODELS

CASE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,180}$")
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
IDENTITY_FIELDS = ("case_id", "family", "contract_id", "contract_code", "input_date", "target_date",
                   "current_price", "current_bar", "question", "currency", "unit", "exchange_timezone", "price_basis")


def _identifier(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value) or value in (".", ".."):
        raise ValueError("题目或数据集编号无效")
    return value


def _contained_file(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError("预测记录文件路径无效")
    return path


def _same_question(left, right):
    if any(left.get(key) != right.get(key) for key in IDENTITY_FIELDS):
        raise ValueError("预测记录与冻结题目或价格基线不匹配")


def _model_id(run):
    model = str(run.get("model") or run.get("strategy_id") or "unknown").split("/")[-1]
    model = model.removeprefix("aihub-")
    return "gpt-6.1-sol" if model == "gpt6.1-sol" else model[:120]


def _stamp(value):
    if not isinstance(value, str):
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        point = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return point.astimezone(timezone.utc) if point.tzinfo is not None else datetime.min.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _empty_row(model, experiment="Exp01"):
    return {"model": model, "experiment_id": experiment, "experiment_name": experiment,
            "run_id": None, "status": "pending", "predicted": None, "actual": None,
            "absolute_errors": None, "direction_correct": None, "submitted_at": None,
            "prediction_locked_at": None, "dataset_id": None, "case_id": None,
            "time_audit": {"integrity_verified": False, "status": "not_submitted"},
            "latest_attempt": None}


def _expected_models(root, errors):
    reference = root / "batches" / "Exp01.json"
    if not reference.exists():
        return list(LIVE_PREDICTION_MODELS)
    try:
        batch = read_json(_contained_file(root, reference))
        names = batch.get("models")
        if not isinstance(names, dict) or not names or any(not isinstance(name, str) or not name for name in names):
            raise ValueError("Exp01 模型清单无效")
        models = [_model_id({"model": name}) for name in names]
        if len(set(models)) != len(models):
            raise ValueError("Exp01 模型清单有重复别名")
        return [model for model in models if model in LIVE_PREDICTION_MODELS]
    except (OSError, ValueError, TypeError) as error:
        errors.append({"run_id": None, "type": type(error).__name__, "message": "Exp01 模型清单读取失败：" + str(error)[:200]})
        return list(LIVE_PREDICTION_MODELS)


def _verified_lock(run_root, run, frozen_question, record):
    case_id = _identifier(record.get("case_id"), CASE_ID)
    if case_id != frozen_question["case_id"]:
        raise ValueError("预测结果题目绑定不一致")
    attempt = _identifier(record.get("attempt"), CASE_ID)
    case_root = run_root / "cases" / case_id
    lock = read_json(_contained_file(run_root, case_root / attempt / "prediction-lock.json"))
    material = read_json(_contained_file(run_root, case_root / attempt / "input.json"))
    if (lock.get("case_id") != case_id or digest(lock) != record.get("prediction_lock_sha256")
            or lock.get("predicted") != record.get("predicted")
            or lock.get("raw_output_sha256") != digest(record.get("raw_output"))
            or lock.get("input_sha256") != digest(material)
            or record.get("input_sha256", lock.get("input_sha256")) != lock.get("input_sha256")):
        raise ValueError("实盘已锁定预测凭证校验失败")
    _same_question(material.get("question", {}), frozen_question)
    predicted = extract_forecast(record["raw_output"], frozen_question["current_price"])
    if predicted != lock["predicted"]:
        raise ValueError("锁定预测与实际模型输出不一致")
    policy = lock.get("submission_deadline_policy")
    if policy not in (None, LIVE_SUBMISSION_POLICY):
        raise ValueError("未知实盘提交截止协议")
    if run.get("submission_deadline_policy") == LIVE_SUBMISSION_POLICY and policy != LIVE_SUBMISSION_POLICY:
        raise ValueError("实盘锁定凭证缺少提交截止协议")
    legacy = policy is None
    _validate_live_open(frozen_question, lock["submitted_at"], legacy=legacy)
    _validate_live_open(frozen_question, lock["prediction_locked_at"], legacy=legacy)
    deadline = live_submission_deadline(frozen_question, legacy=legacy)
    return predicted, lock, {"integrity_verified": True, "status": "verified",
                             "deadline_policy": policy or "legacy-frozen-live-deadline-v1",
                             "submission_deadline_utc": deadline.isoformat(), "legacy_protocol": legacy,
                             "submitted_before_deadline": _stamp(lock["submitted_at"]) < deadline,
                             "locked_before_deadline": _stamp(lock["prediction_locked_at"]) < deadline,
                             "retrieval_time_policy": run.get("retrieval_time_policy"),
                             "retrieval_boundary": (run.get("retrieval_boundaries") or {}).get(case_id)}


def live_question_predictions(benchmarks, evaluations, dataset_id, case_id):
    """Aggregate verified predictions without models, settlements or file writes."""
    _identifier(dataset_id, DATASET_ID)
    _identifier(case_id, CASE_ID)
    history = benchmarks.live_history()
    question = next((item for item in history["questions"] if any(
        version["dataset_id"] == dataset_id and version["case_id"] == case_id
        for version in item["versions"])), None)
    if question is None:
        if dataset_id in history["summary"]["errors"]:
            raise RuntimeError("题目历史完整性校验失败，暂不能读取模型预测")
        raise LookupError("实盘题目或有效发布版本不存在")
    versions = {(item["dataset_id"], item["case_id"]) for item in question["versions"]}
    source_ids = {item[0] for item in versions}
    datasets, grouped, errors = {}, defaultdict(list), []
    for source_id in source_ids:
        datasets[source_id] = benchmarks.load_dataset(source_id, mode="live")
    root = Path(evaluations.root).resolve()
    for run_file in sorted((root / "runs").glob("*/run.json")):
        run = None
        try:
            run_root = run_file.parent
            run_id = _identifier(run_root.name, RUN_ID)
            run = read_json(_contained_file(root, run_file))
            source_id = run.get("source_dataset_id") or run.get("dataset_id")
            if run.get("mode") != "live" or source_id not in source_ids:
                continue
            if run.get("run_id") != run_id or run.get("protocol") not in (*ACTIVE_EVALUATION_PROTOCOLS, PUSH_NEWS_PROTOCOL):
                raise ValueError("实盘运行编号或协议无效")
            frozen = read_json(_contained_file(run_root, run_root / "dataset.json"))
            if digest(frozen) != run.get("dataset_snapshot_sha256") or frozen.get("manifest", {}).get("dataset_id") != source_id:
                raise ValueError("实盘运行冻结测试集校验失败")
            validate_run_retrieval_policy(run, frozen)
            matching = [item for item in frozen["questions"] if (source_id, item.get("case_id")) in versions]
            if not matching:
                continue
            if len(matching) != 1:
                raise ValueError("实盘运行题目绑定不唯一")
            frozen_question = matching[0]
            bound_id = frozen_question["case_id"]
            source_question = next(item for item in datasets[source_id]["questions"] if item["case_id"] == bound_id)
            _same_question(frozen_question, source_question)
            if (isinstance(source_question.get("time_windows"), dict)
                    and source_question["time_windows"] != frozen_question.get("time_windows")):
                raise ValueError("预测交易时段与源版本不匹配")
            if live_submission_deadline(frozen_question, legacy=True) != live_submission_deadline(source_question, legacy=True):
                raise ValueError("预测原始截止与源版本不匹配")
            experiment = run.get("experiment_id")
            model = _model_id(run)
            row = _empty_row(model, experiment)
            row.update(experiment_name=run.get("experiment_name") or experiment, run_id=run_id,
                       dataset_id=source_id, case_id=bound_id, current_price=source_question["current_price"],
                       unit=source_question.get("unit"), status=run.get("status", "pending"),
                       started_at=run.get("started_at"), finished_at=run.get("finished_at"))
            result_file = run_root / "cases" / bound_id / "result.json"
            if not result_file.exists():
                row["status"] = run.get("status") if run.get("status") in ("queued", "running") else "failed"
                row["error"] = run.get("error") or ("尚无有效锁定预测" if row["status"] == "failed" else None)
            else:
                record = read_json(_contained_file(run_root, result_file))
                if record.get("case_id") != bound_id:
                    raise ValueError("实盘结果题目编号不匹配")
                row["status"] = record.get("status", "failed")
                if row["status"] in ("success", "predicted_pending"):
                    predicted, lock, audit = _verified_lock(run_root, run, frozen_question, record)
                    actual = datasets[source_id]["labels"].get(bound_id)
                    score = evaluate_forecast(predicted, actual, source_question["current_price"]) if actual is not None else {}
                    row.update(predicted=predicted, actual=actual, absolute_errors=score.get("absolute_errors"),
                               direction_correct=score.get("direction_correct"), time_audit=audit,
                               status="success" if actual is not None else "predicted_pending",
                               submitted_at=lock["submitted_at"], prediction_locked_at=lock["prediction_locked_at"])
                else:
                    row["error"] = str(record.get("error") or "本次作答未形成有效锁定预测")[:300]
            grouped[(model, experiment)].append(row)
        except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
            # Ignore unrelated malformed runs, but explicitly report every
            # damaged record whose outer metadata binds it to this question.
            if isinstance(run, dict) and run.get("mode") == "live" and (run.get("source_dataset_id") or run.get("dataset_id")) in source_ids:
                detail = {"run_id": run_file.parent.name, "type": type(error).__name__, "message": str(error)[:300]}
                errors.append(detail)
                model, experiment = _model_id(run), run.get("experiment_id")
                failed = _empty_row(model, experiment)
                failed.update(run_id=run_file.parent.name, status="failed", error=detail["message"],
                              started_at=run.get("started_at"), time_audit={"integrity_verified": False, "status": "invalid"})
                grouped[(model, experiment)].append(failed)
    expected_models = _expected_models(root, errors)
    rows = {(model, "Exp01"): _empty_row(model) for model in expected_models}
    for key, attempts in grouped.items():
        attempts.sort(key=lambda item: (_stamp(item.get("prediction_locked_at") or item.get("finished_at") or item.get("started_at")), item["run_id"] or ""), reverse=True)
        locked = [item for item in attempts if item.get("predicted") is not None and item["time_audit"].get("integrity_verified")]
        selected = dict(locked[0] if locked else attempts[0])
        latest = attempts[0]
        if latest["run_id"] != selected["run_id"]:
            selected["latest_attempt"] = {key: latest.get(key) for key in ("run_id", "status", "error", "started_at", "finished_at")}
        selected["attempt_count"] = len(attempts)
        rows[key] = selected
    ordered = list(rows.values())
    expected_order = {model: index for index, model in enumerate(AIHUB_MODELS)}
    ordered.sort(key=lambda row: (row["experiment_id"] != "Exp01", expected_order.get(row["model"], len(expected_order)), row["model"], row["experiment_id"] or ""))
    summary = {"expected_models": len(expected_models), "predicted": sum(row["predicted"] is not None for row in ordered),
               "settled": sum(row["status"] == "success" for row in ordered),
               "failed": sum(row["status"] == "failed" for row in ordered),
               "waiting": sum(row["status"] in ("pending", "queued", "running") for row in ordered)}
    return {"mode": "live", "dataset_id": dataset_id, "case_id": case_id,
            "question": {key: question.get(key) for key in ("dataset_id", "case_id", "family", "contract_id", "input_date", "target_date", "current_price", "unit", "actual", "forecast_deadline")},
            "items": ordered, "total": len(ordered), "summary": summary, "errors": errors,
            "history_errors": history["summary"]["errors"]}
