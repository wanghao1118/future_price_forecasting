"""Trading Return environments and the deterministic daily trading ledger.

The benchmark stores market questions and realized bars.  This module owns the
environment-specific prompt contract and the host-side scoring rules so a BMK
question never decides which fields a model must emit.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
import math
import re


ENV_V1 = "env-v1"
ENV_V2 = "env-v2"
ENV_IDS = (ENV_V1, ENV_V2)
LEGACY_ENV_IDS = {"eval-v1": ENV_V1, "eval-v2": ENV_V2}

TRADING_DECISION_FIELDS = ("buy_long", "buy_short", "sell_long", "sell_short")

TRADING_RULES = {
    "AU": {
        "name": "沪金 AU",
        "exchange": "上期所",
        "currency": "CNY",
        "unit": "克",
        "contract_multiplier": 1000,
        "tick_size": 0.02,
        "base_slippage_ticks": 1,
        "stress_slippage_ticks": 2,
        "exchange_open_fee": 20.0,
        "exchange_close_fee": 0.0,
        "broker_fee_per_side": 2.0,
        "initial_capital": 5_000_000.0,
        "max_exposure_ratio": 1.0,
        "margin_rate": 0.12,
    },
    "SC": {
        "name": "原油 SC",
        "exchange": "上海国际能源交易中心",
        "currency": "CNY",
        "unit": "桶",
        "contract_multiplier": 1000,
        "tick_size": 0.1,
        "base_slippage_ticks": 1,
        "stress_slippage_ticks": 2,
        "exchange_open_fee": 20.0,
        "exchange_close_fee": 0.0,
        "broker_fee_per_side": 2.0,
        "initial_capital": 5_000_000.0,
        "max_exposure_ratio": 1.0,
        "margin_rate": 0.12,
    },
}

ENVIRONMENT_SPECS = {
    ENV_V1: {
        "id": ENV_V1,
        "name": "Env v1",
        "kind": "prediction",
        "model": "gpt-6.1-sol",
        "description": "日频预测环境。模型根据当日已知信息预测下一交易日的方向、涨跌幅和高低点。",
        "metric_description": "方向准确率、涨跌幅 MAE、最高价 MAE、最低价 MAE。",
        "injection_prompt": (
            "请输出 JSON，字段为 direction、return_pct、high、low。direction 只能是 up、down 或 flat。"
            "return_pct 是百分数，high 和 low 是下一交易日最高价和最低价。"
        ),
        "output_schema": {
            "direction": {"label": "下一日涨跌方向", "type": "enum", "values": ["up", "down", "flat"]},
            "return_pct": {"label": "下一日涨跌幅", "type": "number", "unit": "percent"},
            "high": {"label": "下一日最高价", "type": "number"},
            "low": {"label": "下一日最低价", "type": "number"},
        },
    },
    ENV_V2: {
        "id": ENV_V2,
        "name": "Env v2",
        "kind": "trading",
        "model": "gpt-6.1-sol",
        "description": "日频交易环境。模型每天根据题目和账户状态决定买多、买空、卖多、卖空或空仓，环境按规则核算收益。",
        "metric_description": "最终资金、累计收益率、最大回撤、最大单日亏损、手续费、滑点和交易天数。",
        "injection_prompt": (
            "这是日频交易环境。请输出 JSON，字段为 buy_long、buy_short、sell_long、sell_short，"
            "四个字段都是非负整数，表示手数。buy_long 与 buy_short 不得同时大于 0，"
            "sell_long 与 sell_short 不得同时大于 0。买多表示开多，买空表示开空，"
            "卖多表示平多，卖空表示平空；四项都为 0 表示当天不交易。"
            "环境会按固定资金、合约乘数、滑点、手续费、保证金和持仓限制计算每日净收益。"
        ),
        "output_schema": {
            "buy_long": {"label": "买多手数", "type": "integer", "minimum": 0},
            "buy_short": {"label": "买空手数", "type": "integer", "minimum": 0},
            "sell_long": {"label": "卖多手数", "type": "integer", "minimum": 0},
            "sell_short": {"label": "卖空手数", "type": "integer", "minimum": 0},
        },
    },
}


def normalize_environment_id(value):
    if value is None:
        return ENV_V1
    value = LEGACY_ENV_IDS.get(value, value)
    if value not in ENV_IDS:
        raise ValueError("环境不在 Env Server 可用目录中")
    return value


def environment_spec(environment_id=ENV_V1):
    return deepcopy(ENVIRONMENT_SPECS[normalize_environment_id(environment_id)])


def environment_injection(environment_id=ENV_V1):
    return environment_spec(environment_id)["injection_prompt"]


def project_environment_question(case, environment_id=ENV_V1):
    """Remove stale BMK output wording and attach the selected Env contract."""
    environment_id = normalize_environment_id(environment_id)
    result = deepcopy(case)
    question = result.get("question")
    if isinstance(question, str):
        result["question"] = re.sub(r"请预测[\s\S]*?返回这四项具体结果。", "请预测下一交易日的行情表现。", question)
    result["environment_id"] = environment_id
    result["environment_injection"] = environment_injection(environment_id)
    # Historical clients still read these names.  The canonical owner is Env Server.
    result["evaluator_id"] = "eval-v1" if environment_id == ENV_V1 else "eval-v2"
    result["evaluator_injection"] = result["environment_injection"]
    return result


def _structured_output(output):
    if isinstance(output, dict):
        return output
    if not isinstance(output, str):
        raise ValueError("模型输出必须是 JSON 对象")
    text = output.strip()
    try:
        value = json.loads(text)
    except ValueError:
        blocks = re.findall(r"```(?:json)?\s*\n?(.*?)```", text, flags=re.S | re.I)
        values = []
        for block in blocks:
            try:
                values.append(json.loads(block.strip()))
            except ValueError:
                continue
        if len(values) != 1:
            raise ValueError("模型必须返回唯一的 JSON 对象")
        value = values[0]
    if not isinstance(value, dict):
        raise ValueError("模型输出必须是 JSON 对象")
    return value


def _finite_number(value, label):
    if isinstance(value, bool):
        raise ValueError(label + " 必须是有限数值")
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        value = int(value.strip())
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(label + " 必须是有限数值")
    return value


def extract_trade_decision(output):
    value = _structured_output(output)
    missing = [field for field in TRADING_DECISION_FIELDS if field not in value]
    if missing:
        raise ValueError("模型输出缺少交易字段：" + ", ".join(missing))
    decision = {}
    for field in TRADING_DECISION_FIELDS:
        number = _finite_number(value[field], field)
        if int(number) != number or number < 0 or number > 1_000_000:
            raise ValueError(field + " 必须是 0 至 1000000 的整数")
        decision[field] = int(number)
    if decision["buy_long"] and decision["buy_short"]:
        raise ValueError("买多和买空不能同时大于 0")
    if decision["sell_long"] and decision["sell_short"]:
        raise ValueError("卖多和卖空不能同时大于 0")
    if isinstance(value.get("reason"), str):
        decision["reason"] = value["reason"][:2000]
    if isinstance(value.get("public_analysis"), (dict, str)):
        decision["public_analysis"] = deepcopy(value["public_analysis"])
    return decision


def _bar_number(bar, field):
    value = bar.get(field) if isinstance(bar, dict) else None
    value = _finite_number(value, "行情 " + field)
    if float(value) <= 0:
        raise ValueError("行情 " + field + " 必须大于 0")
    return float(value)


def _date_key(bar, index):
    value = bar.get("date") or bar.get("trading_date") or bar.get("target_date")
    return str(value) if value is not None else "day-{:06d}".format(index)


def _decision_for(decisions, key, index):
    if isinstance(decisions, dict):
        value = decisions.get(key) or decisions.get(str(index))
    elif isinstance(decisions, list):
        value = decisions[index] if index < len(decisions) else None
    else:
        value = None
    return extract_trade_decision(value or {field: 0 for field in TRADING_DECISION_FIELDS})


def simulate_trading(bars, decisions, market="SC", initial_capital=None, slippage_scenario="base"):
    """Run the rules document's daily open/close ledger on model decisions."""
    market = str(market or "SC").upper()
    if market not in TRADING_RULES:
        raise ValueError("交易环境当前支持 AU 和 SC")
    rules = deepcopy(TRADING_RULES[market])
    if initial_capital is None:
        initial_capital = rules["initial_capital"]
    initial_capital = float(_finite_number(initial_capital, "初始资金"))
    if initial_capital <= 0:
        raise ValueError("初始资金必须大于 0")
    ticks = rules["stress_slippage_ticks"] if slippage_scenario == "stress" else rules["base_slippage_ticks"]
    if slippage_scenario not in ("base", "stress"):
        raise ValueError("滑点场景必须是 base 或 stress")
    bars = list(bars or [])
    if not bars:
        raise ValueError("至少需要一条日线行情")
    cash = initial_capital
    peak = initial_capital
    records = []
    rejected = 0
    risk_limited = 0
    cumulative_fees = 0.0
    cumulative_slippage = 0.0
    total_turnover = 0.0
    position = {"long": 0, "short": 0}
    for index, bar in enumerate(bars):
        key = _date_key(bar, index)
        opening = _bar_number(bar, "open")
        high = _bar_number(bar, "high")
        low = _bar_number(bar, "low")
        closing = _bar_number(bar, "close")
        if high < max(opening, closing) or low > min(opening, closing) or high < low:
            raise ValueError("行情高低点与开收盘价不一致：" + key)
        decision = _decision_for(decisions, key, index)
        equity_before = cash
        close_long = min(position["long"], decision["sell_long"])
        close_short = min(position["short"], decision["sell_short"])
        position["long"] -= close_long
        position["short"] -= close_short
        entry_long = decision["buy_long"]
        entry_short = decision["buy_short"]
        if entry_long and entry_short:
            rejected += 1
            entry_long = entry_short = 0
        entry_price = opening * rules["contract_multiplier"]
        max_by_exposure = int(equity_before * rules["max_exposure_ratio"] // entry_price)
        max_by_margin = int(equity_before * 0.30 // (entry_price * rules["margin_rate"]))
        allowed_lots = max(0, min(max_by_exposure, max_by_margin))
        requested = entry_long or entry_short
        executed = min(requested, allowed_lots)
        if executed < requested:
            risk_limited += 1
        entry_long = executed if entry_long else 0
        entry_short = executed if entry_short else 0
        # A same-day decision may explicitly pair an opening action with its
        # closing action. The rules still flatten any remainder at session end.
        if decision["sell_long"] > position["long"] + entry_long or decision["sell_short"] > position["short"] + entry_short:
            rejected += 1
        slippage = rules["tick_size"] * ticks
        pnl = 0.0
        fees = 0.0
        slippage_cost = 0.0
        trades = []
        if close_long:
            exit_price = closing - slippage
            pnl += (exit_price - closing) * rules["contract_multiplier"] * close_long
            fees += close_long * (rules["exchange_close_fee"] + rules["broker_fee_per_side"])
            slippage_cost += slippage * rules["contract_multiplier"] * close_long
            total_turnover += closing * rules["contract_multiplier"] * close_long
            trades.append({"action": "sell_long", "lots": close_long, "price": exit_price})
        if close_short:
            exit_price = closing + slippage
            pnl += (closing - exit_price) * rules["contract_multiplier"] * close_short
            fees += close_short * (rules["exchange_close_fee"] + rules["broker_fee_per_side"])
            slippage_cost += slippage * rules["contract_multiplier"] * close_short
            total_turnover += closing * rules["contract_multiplier"] * close_short
            trades.append({"action": "sell_short", "lots": close_short, "price": exit_price})
        if entry_long:
            entry_price_value = opening + slippage
            exit_price = closing - slippage
            pnl += (exit_price - entry_price_value) * rules["contract_multiplier"] * entry_long
            fees += entry_long * (rules["exchange_open_fee"] + rules["broker_fee_per_side"])
            fees += entry_long * (rules["exchange_close_fee"] + rules["broker_fee_per_side"])
            slippage_cost += slippage * rules["contract_multiplier"] * entry_long * 2
            total_turnover += (opening + closing) * rules["contract_multiplier"] * entry_long
            trades.append({"action": "buy_long", "lots": entry_long, "entry": entry_price_value, "exit": exit_price})
        if entry_short:
            entry_price_value = opening - slippage
            exit_price = closing + slippage
            pnl += (entry_price_value - exit_price) * rules["contract_multiplier"] * entry_short
            fees += entry_short * (rules["exchange_open_fee"] + rules["broker_fee_per_side"])
            fees += entry_short * (rules["exchange_close_fee"] + rules["broker_fee_per_side"])
            slippage_cost += slippage * rules["contract_multiplier"] * entry_short * 2
            total_turnover += (opening + closing) * rules["contract_multiplier"] * entry_short
            trades.append({"action": "buy_short", "lots": entry_short, "entry": entry_price_value, "exit": exit_price})
        # The rules require all newly opened intraday positions to be closed by the session end.
        position["long"] = position["short"] = 0
        net = pnl - fees
        cash += net
        peak = max(peak, cash)
        drawdown = (peak - cash) / peak * 100 if peak else 0.0
        cumulative_fees += fees
        cumulative_slippage += slippage_cost
        records.append({"date": key, "decision": decision, "pnl": pnl, "fees": fees,
                        "slippage": slippage_cost, "net_pnl": net, "equity": cash,
                        "return_pct": net / equity_before * 100 if equity_before else 0.0,
                        "drawdown_pct": drawdown, "trades": trades,
                        "requested_lots": requested, "executed_lots": executed})
    max_drawdown = max((row["drawdown_pct"] for row in records), default=0.0)
    max_loss = min((row["net_pnl"] for row in records), default=0.0)
    return {"market": market, "rules_version": "黄金与原油期货交易台规则-v0.1",
            "initial_capital": initial_capital, "final_equity": cash,
            "profit": cash - initial_capital, "total_return_pct": (cash / initial_capital - 1) * 100,
            "max_drawdown_pct": max_drawdown, "max_single_day_loss": max_loss,
            "trading_days": sum(bool(row["trades"]) for row in records),
            "decision_days": len(records), "cumulative_fees": cumulative_fees,
            "cumulative_slippage": cumulative_slippage, "turnover": total_turnover,
            "rejected_actions": rejected, "risk_limited_actions": risk_limited,
            "daily": records, "rules": rules}


def evaluate_trade_case(decision, question, actual, initial_capital=None):
    """Evaluate one daily decision and return a case-shaped scoring record."""
    family = str((question or {}).get("family") or "SC").upper()
    market = "AU" if family == "AU" else "SC"
    current = (question or {}).get("current_bar") or {}
    actual = actual if isinstance(actual, dict) else {}
    bar = {"date": actual.get("target_date") or question.get("target_date"),
           "open": actual.get("open") or question.get("current_price"),
           "high": actual.get("high"), "low": actual.get("low"), "close": actual.get("close")}
    # Historical BMK labels created before Env v2 may not carry target open.
    # In that case use the last known close and mark the input basis explicitly.
    result = simulate_trading([bar], [decision], market=market, initial_capital=initial_capital)
    result["entry_price_basis"] = "target_open" if actual.get("open") is not None else "current_close_fallback"
    result["current_bar"] = deepcopy(current)
    return {"predicted": decision, "actual": actual, "trade_metrics": {
        key: result[key] for key in ("profit", "final_equity", "total_return_pct", "cumulative_fees",
                                     "cumulative_slippage", "trading_days", "max_drawdown_pct",
                                     "max_single_day_loss", "rejected_actions", "risk_limited_actions")
    }, "entry_price_basis": result["entry_price_basis"]}


def aggregate_trade_metrics(cases, questions, initial_capital=5_000_000.0):
    rows = {row.get("case_id"): row for row in cases if isinstance(row, dict)}
    question_map = {row.get("case_id"): row for row in questions if isinstance(row, dict)}
    bars, decisions = [], []
    pending = failed = 0
    for case_id, row in rows.items():
        if row.get("status") == "predicted_pending":
            pending += 1
            continue
        if row.get("status") != "success" or not isinstance(row.get("predicted"), dict):
            failed += 1
            continue
        actual = row.get("actual") if isinstance(row.get("actual"), dict) else {}
        question = question_map.get(case_id, row)
        bars.append({"date": actual.get("target_date") or question.get("target_date") or case_id,
                     "open": actual.get("open") or question.get("current_price"),
                     "high": actual.get("high"), "low": actual.get("low"), "close": actual.get("close")})
        decisions.append(row["predicted"])
    if not bars:
        return [{"metric": "trading_environment", "count": 0, "total": len(questions),
                 "pending": pending, "failed": failed, "eligible_for_ranking": False,
                 "initial_capital": initial_capital}]
    # Cases from multiple families are kept as independent ledgers.
    ledgers = []
    for family in sorted({str((question_map.get(row.get("case_id"), row)).get("family") or "SC").upper()
                          for row in rows.values()}):
        family_rows = [(row, question_map.get(row.get("case_id"), row)) for row in rows.values()
                       if str((question_map.get(row.get("case_id"), row)).get("family") or "SC").upper() == family
                       and row.get("status") == "success" and isinstance(row.get("predicted"), dict)]
        if not family_rows:
            continue
        family_bars = []
        family_decisions = []
        for row, question in sorted(family_rows, key=lambda pair: str(pair[1].get("target_date") or pair[0].get("case_id"))):
            actual = row.get("actual") or {}
            family_bars.append({"date": actual.get("target_date") or question.get("target_date") or row.get("case_id"),
                                "open": actual.get("open") or question.get("current_price"),
                                "high": actual.get("high"), "low": actual.get("low"), "close": actual.get("close")})
            family_decisions.append(row["predicted"])
        ledgers.append(simulate_trading(family_bars, family_decisions, market="AU" if family == "AU" else "SC",
                                        initial_capital=initial_capital))
    return [{"metric": "trading_environment", "count": len(bars), "total": len(questions),
             "pending": pending, "failed": failed, "eligible_for_ranking": not pending and not failed,
             "initial_capital": initial_capital,
             "final_equity": sum(item["final_equity"] for item in ledgers),
             "profit": sum(item["profit"] for item in ledgers),
             "total_return_pct": sum(item["total_return_pct"] for item in ledgers) / len(ledgers),
             "max_drawdown_pct": max(item["max_drawdown_pct"] for item in ledgers),
             "max_single_day_loss": min(item["max_single_day_loss"] for item in ledgers),
             "cumulative_fees": sum(item["cumulative_fees"] for item in ledgers),
             "cumulative_slippage": sum(item["cumulative_slippage"] for item in ledgers),
             "trading_days": sum(item["trading_days"] for item in ledgers),
             "rejected_actions": sum(item["rejected_actions"] for item in ledgers),
             "risk_limited_actions": sum(item["risk_limited_actions"] for item in ledgers),
             "ledgers": ledgers}]
