"""A fixed external tool surface, delegating authority to the original executor."""

from __future__ import annotations

from copy import deepcopy

from .tool_protocol import ToolCall, prepare_tool_turn


GUIDANCE = (
    "【客户端工具入口】本轮客户端固定提供describe_tools和invoke_tool。"
    "原工具（含委托、交还、复核和交付）统一通过invoke_tool的name和arguments调用；"
    "需要原参数定义时用describe_tools按names读取，省略names只列用途节选。"
    "已知定义可直接调用，不必重复读取；读取目录不是实际执行或验收，原权限与终态仍有效。\n"
)


def validate_surface(value):
    if type(value) is not str or value not in {"all", "catalog"}:
        raise ValueError("tool_surface must be all or catalog")
    return value


class ExternalToolCatalog:
    """Run-local descriptions, with no permission grants or execution replay."""

    def __init__(self, tools, execute):
        prepare_tool_turn(role="external_catalog", prompt="", history=[], tools=tools)
        if not tools or not callable(execute):
            raise ValueError("A nonempty catalog and executor are required")
        self._definitions = {tool["name"]: deepcopy(tool) for tool in tools}
        self._execute = execute
        names = list(self._definitions)
        self.tools = [
            {"name": "describe_tools", "description": "读取本轮实际工具定义：省略names列名称与用途节选，提供names读完整参数。只读，不执行也不授予权限。",
             "input_schema": {"type": "object", "properties": {"names": {"type": "array", "minItems": 1,
                "maxItems": len(names), "uniqueItems": True, "items": {"type": "string", "enum": names}}}, "additionalProperties": False}},
            {"name": "invoke_tool", "description": "调用本轮实际工具，name选原工具名称，arguments按其原定义填写对象。需要定义可先describe_tools；原权限、参数校验和交接终态不变，不是任意代码执行入口。",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string", "enum": names},
                "arguments": {"type": "object"}}, "required": ["name", "arguments"], "additionalProperties": False}},
        ]

    @staticmethod
    def _invalid():
        return {"status": "error", "complete": True, "outcome": "not_started",
                "code": "invalid_catalog_arguments", "message": "请选择本轮工具并按定义提供参数；未执行动作。"}

    def dispatch(self, call):
        arguments = call.arguments
        if not isinstance(arguments, dict):
            return self._invalid()
        if call.name == "describe_tools":
            if set(arguments) - {"names"}:
                return self._invalid()
            if "names" not in arguments:
                return {"status": "ok", "tools": [{"name": name,
                    "description_excerpt": tool["description"][:240],
                    "description_truncated": len(tool["description"]) > 240}
                    for name, tool in self._definitions.items()]}
            names = arguments["names"]
            if (not isinstance(names, list) or not names or len(names) > len(self._definitions)
                    or any(type(name) is not str or name not in self._definitions for name in names)
                    or len(names) != len(set(names))):
                return self._invalid()
            return {"status": "ok", "definitions": [deepcopy(self._definitions[name]) for name in names]}
        if call.name != "invoke_tool" or set(arguments) != {"name", "arguments"}:
            return self._invalid()
        name = arguments["name"]
        if type(name) is not str or name not in self._definitions or not isinstance(arguments["arguments"], dict):
            return self._invalid()
        return self._execute(ToolCall(call.id, name, deepcopy(arguments["arguments"])))
