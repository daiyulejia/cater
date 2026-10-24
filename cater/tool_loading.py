"""Optional run-local schema loading, never an execution or permission grant."""

from __future__ import annotations


TOOL = "load_tools"
CORE = frozenset({TOOL, "wait_for_reply", "send_message", "continue_work", "return_work",
                  "set_objective", "finish_work", "reply_to_customer", "read_receipts",
                  "list_skills", "read_skill"})


def validate_mode(value):
    if type(value) is not str or value not in {"all", "on_demand"}:
        raise ValueError("tool_loading must be all or on_demand")
    return value


def with_loader(tools):
    names = [tool["name"] for tool in tools]
    if TOOL in names:
        raise ValueError("Duplicate tool loader")
    if not names:
        return tools
    return [*tools, {"name": TOOL,
        "description": "按tool_catalog中的确切名称载入所需工具，可一次选择多项。下一次模型请求会提供完整参数定义；本次不执行这些工具、不增加权限，也无需载入全部工具。",
        "input_schema": {"type": "object", "properties": {
            "names": {"type": "array", "minItems": 1, "maxItems": len(names),
                      "uniqueItems": True, "items": {"type": "string", "enum": names}}},
            "required": ["names"], "additionalProperties": False}}]


def load_result(arguments, schema):
    names = arguments.get("names") if isinstance(arguments, dict) else None
    allowed = schema["properties"]["names"]["items"]["enum"]
    if (not isinstance(arguments, dict) or set(arguments) != {"names"}
            or not isinstance(names, list) or not 1 <= len(names) <= len(allowed)
            or any(type(name) is not str or name not in allowed for name in names)
            or len(set(names)) != len(names)):
        return {"status": "error", "code": "invalid_arguments", "complete": True,
                "outcome": "not_started", "message": "请选择当前目录内不重复的工具名称；未载入或执行任何工具。"}
    return {"status": "ok", "loaded_names": list(names), "effective": "next_model_request",
            "message": "下一次请求提供完整参数定义；本次只载入声明，没有执行工具动作或增加权限。"}


def select_tools(tools, loaded):
    if not any(tool["name"] == TOOL for tool in tools):
        return tools, None
    selected = [tool for tool in tools if tool["name"] in CORE or tool["name"] in loaded]
    return selected, {"mode": "on_demand", "loader": TOOL,
        "available_names": [tool["name"] for tool in tools if tool["name"] != TOOL],
        "loaded_names": [tool["name"] for tool in selected if tool["name"] != TOOL],
        "definitions": "full_on_next_request"}
