"""Stable text formats for persisted formal review reports."""

CURRENT_VERSION = 2
SUPPORTED_VERSIONS = (1, 2)


def review_report_text(verdict, feedback, review_seq, *, version=CURRENT_VERSION):
    """Render exactly one historical format; do not normalize persisted feedback."""
    if type(version) is not int or version not in SUPPORTED_VERSIONS:
        raise ValueError("Unknown review report format version")
    if verdict not in ("approved", "changes_requested") or not isinstance(feedback, str):
        raise ValueError("Invalid review report")
    if type(review_seq) is not int or review_seq < 1:
        raise ValueError("Invalid review request sequence")
    text = ("复核通过。" if verdict == "approved" else "本轮需要修复，尚未验收。") + feedback
    if verdict == "approved":
        return text + ("\n请向原委托人交回这版成果。若继续修改，需将新版再次交回复核。" if version == 1 else
                       "\n请收齐这版成果与相关工作回报，再向原委托人交回或由店长整体交付。若继续修改，需将新版再次交回复核。")
    return text + (f"\n请修改并发布新版后，调用request_review，review_seq={review_seq}，仍交给我复核；不把修复自述当成验收。" if version == 1 else
                   f"\n请协调实际修复并取得新版后，调用request_review，review_seq={review_seq}，仍交给我复核；不把修复自述当成验收。")
