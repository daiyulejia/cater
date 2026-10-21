"""One native response-format correction, never a replay of executed tools."""
from .tool_protocol import ToolTurn, contained_response_diagnostics


def correctable(driver, turn):
    if type(getattr(driver, "tool_response_corrections", None)) is not int or driver.tool_response_corrections != 1:
        return False
    if (not isinstance(turn, ToolTurn) or type(turn.meta) is not dict
            or turn.meta.get("contained") is not True or turn.meta.get("incomplete") is not True
            or turn.meta.get("invocation_started") is not True
            or turn.text != "" or turn.calls != []
            or turn.message != {"role": "assistant", "content": "", "tool_calls": []}):
        return False
    mode = turn.meta.get("mode")
    stop = turn.meta.get("finish_reason") if mode == "openai_compatible" else turn.meta.get("stop_reason")
    allowed = ("stop", "tool_calls") if mode == "openai_compatible" else ("end_turn", "stop_sequence", "tool_use")
    if type(stop) is not str or stop not in allowed:
        return False
    return "response_tool_issue" in contained_response_diagnostics(turn.meta)


def correction_context(model_call_seq, issue):
    return {"model_call_seq": model_call_seq, "issue": issue, "attempt": 1,
        "instruction": (
            "上一条响应的工具请求未通过校验，整条响应的宿主工具均未执行，不是任务已完成。"
            "请保留原目标、委托以及先前成功的工具历史和回执，只纠正这一响应。"
            "只使用本轮声明的客户端工具，每次调用使用新的唯一标识；"
            "参数按协议提供有效且无重复键的JSON对象（Chat Completions使用JSON文本）。"
            "不要重复已经成功的写入、命令、发布或消息投递；不因纠正格式自行扩大权限或目标。"
            "核对当前上下文后继续必要的交接或回复，无需让顾客为格式错误重新派活。")}
