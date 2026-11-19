"""Host-owned tool permissions; model arguments cannot select an environment."""

ENV00_PROTOCOL = "env00-indicators-close-direction-return-range-v1-utc-30m"
ENV00_TOOL_MODE = "agent_initiated_indicators_only"
ENV00_EXTEND_PROTOCOL = "env00-extend-memory-closed-book-v1"
ENV00_EXTEND_TOOL_MODE = "closed_book_no_tools"
ENV02_PROTOCOL = "env02-no-tools-close-direction-return-range-v1"
ENV02_TOOL_MODE = "closed_book_no_tools"
ENV03_PROTOCOL = "env03-agent-search-close-direction-return-range-v1-utc-30m-s01"
ENV03_TOOL_MODE = "agent_initiated_local_search_with_s01"
ENV04_PROTOCOL = "env04-agent-search-close-direction-return-range-v1-utc-30m-s02"
ENV04_TOOL_MODE = "agent_initiated_local_search_with_s02"
NEWS_SEARCH_ENVIRONMENTS = ("env01", "env03", "env04")
SKILL_BY_ENVIRONMENT = {"env03": "s01", "env04": "s02"}


def memory_environment():
    return {"id": "env00-extend", "name": "env00-extend", "protocol": ENV00_EXTEND_PROTOCOL,
            "description": "记忆泄漏题库的独立闭卷环境，仅提供题干；time gate agent 禁止全部联网、新闻及技术检索，Codex CLI evaluator 独立判分。",
            "benchmark_kind": "memory-leakage", "metric": "accuracy", "network_search": False,
            "tools": [], "model_input_fields": ["question"], "tool_mode": ENV00_EXTEND_TOOL_MODE,
            "agents": [{"id": "time-gate-agent", "name": "time gate agent", "role": "宿主强制禁用全部工具与检索，逐次校验模型请求和响应并保存回执。"},
                       {"id": "evaluator-agent", "name": "evaluator agent", "runner": "Codex CLI",
                        "role": "作答锁定后，用独立会话对照题干、参考答案和模型回答逐题判断正确与否。"}]}


def environment_id(context):
    value = context.get("environment_id", "env01")
    if value not in ("env01", "env00", "env00-extend", "env02", "env03", "env04"):
        raise ValueError("未知评估环境")
    return value


def allowed_tools(context):
    environment = environment_id(context)
    if environment in ("env00-extend", "env02"):
        return ()
    return ("search_indicators",) if environment == "env00" else ("search_news", "search_indicators")


def tool_mode(context):
    environment = environment_id(context)
    if environment in ("env00-extend", "env02"):
        return ENV00_EXTEND_TOOL_MODE
    return {"env03": ENV03_TOOL_MODE, "env04": ENV04_TOOL_MODE}.get(
        environment, ENV00_TOOL_MODE if environment == "env00" else "agent_initiated_local_search")


def skill_id(context):
    return SKILL_BY_ENVIRONMENT.get(environment_id(context))


def is_news_environment(value):
    environment = value if isinstance(value, str) else environment_id(value)
    return environment in NEWS_SEARCH_ENVIRONMENTS
