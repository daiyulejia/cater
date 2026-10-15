"""Small text revisions of immutable inputs; no filesystem or command authority."""

import hashlib

from .artifacts import MAX_DELIVERABLES, normalize_deliverables, safe_filename


def revise_artifacts(edits, context):
    if not isinstance(edits, list) or not 1 <= len(edits) <= 16 or context.get("phase") is not None:
        raise ValueError("局部修改需要1至16项，只能由本次成果负责人提交。")
    available = {a["seq"]: a for group in context.get("accepted_inputs", {}).values() for a in group}
    available.update({a["seq"]: a for a in context.get("previous_attempt", {}).get("artifacts", [])})
    revised = {}
    for edit in edits:
        if (not isinstance(edit, dict) or not {"artifact_seq", "old_text", "new_text"} <= set(edit)
                or set(edit) - {"artifact_seq", "expected_sha256", "old_text", "new_text"}):
            raise ValueError("局部修改须指明原成果编号及精确替换前后正文。")
        seq = edit["artifact_seq"]
        source = available.get(seq) if type(seq) is int else None
        if source is None or not isinstance(source.get("content"), str):
            raise ValueError("只能修改本次收到的已验收输入或自己的上次成果。")
        if safe_filename(source.get("filename")) is None or source.get("format") not in {"text", "code", "markdown"}:
            raise ValueError("原成果缺少可交付的安全文件名或文本格式。")
        content = source["content"]
        actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if source.get("content_sha256") != actual or edit.get("expected_sha256", actual) != actual:
            raise ValueError("局部修改的原成果摘要不一致，需要读取正确版本。")
        old, new = edit["old_text"], edit["new_text"]
        if (not isinstance(old, str) or not old or not isinstance(new, str)
                or len(old.encode("utf-8")) > 65536 or len(new.encode("utf-8")) > 65536):
            raise ValueError("替换内容必须是有界文本，原文不能为空。")
        current = revised.get(seq, content)
        if current.count(old) != 1:
            raise ValueError("替换原文必须精确且只出现一次，不能猜测修改位置。")
        current = current.replace(old, new, 1)
        if not current.strip() or len(current.encode("utf-8")) > 65536:
            raise ValueError("修改后的成果为空或过大。")
        revised[seq] = current
    return [{"title": available[seq].get("title", "修订成果"),
             "summary": "已基于明确版本完成局部修改，尚需实际检查与独立复核。",
             "filename": available[seq]["filename"], "format": available[seq]["format"], "content": content}
            for seq, content in revised.items()]


def prepare_snapshot(rt, task_id, owner, edits, deliverables, context):
    """Build a complete snapshot and provenance from host records, before normalization."""
    from .work import WorkConflict

    view, current = rt.work._item(task_id, context["work_item_id"])
    if (not view.get("scope_current") or current["status"] != "running" or current["owner"] != owner
            or any(context.get(k) != current.get(k) for k in ("attempt_id", "inputs"))
            or context.get("plan_seq") != view["plan_seq"] or context.get("scope") != view["scope"]):
        raise ValueError("Artifact edits require the current owner's work claim.")
    events = rt.log.read(task_id=task_id)
    by_seq = {e.seq: e for e in events}
    submissions = {}
    accepted = {}
    try:
        dependencies = rt.work.accepted_dependencies(task_id, current)
    except WorkConflict as exc:
        raise ValueError(str(exc)) from exc
    for dependency in dependencies:
        submission = dependency["submission"]
        accepted[dependency["id"]] = rt.work.artifacts(task_id, submission)
        for artifact in accepted[dependency["id"]]:
            submissions[artifact["seq"]] = (submission, "accepted_input")
    previous = []
    if context.get("previous_attempt"):
        prior = next((e for e in reversed(events) if e.type == "work.submitted"
                      and e.payload.get("work_item_id") == current["id"] and e.actor.id == owner
                      and e.payload.get("plan_seq") == view["plan_seq"]
                      and e.payload.get("scope") == view["scope"]), None)
        if prior is None:
            raise ValueError("Previous snapshot is not this owner's latest submission.")
        submission = {**prior.payload, "seq": prior.seq}
        previous = rt.work.artifacts(task_id, submission)
        if {a["seq"] for a in previous} != {a["seq"] for a in context["previous_attempt"].get("artifacts", [])}:
            raise ValueError("Previous snapshot has changed.")
        for artifact in previous:
            submissions[artifact["seq"]] = (submission, "previous_attempt")
    supplied = {a["seq"] for group in context.get("accepted_inputs", {}).values() for a in group}
    accepted = {key: [a for a in group if a["seq"] in supplied] for key, group in accepted.items()}
    trusted = {"accepted_inputs": accepted, "previous_attempt": {"artifacts": previous},
               "phase": context.get("phase")}
    revisions = revise_artifacts(edits, trusted)
    if deliverables is None:
        deliverables = []
    if not isinstance(deliverables, list) or any(not isinstance(d, dict) for d in deliverables):
        raise ValueError("Deliverables must be a list of files.")
    patch_names = [d["filename"].casefold() for d in revisions]
    explicit_names = [safe_filename(d.get("filename")) for d in deliverables]
    if (len(set(patch_names)) != len(patch_names)
            or set(patch_names) & {n.casefold() for n in explicit_names if n}):
        raise ValueError("The same file cannot be patched and rewritten together.")
    replaced = set(patch_names) | {n.casefold() for n in explicit_names if n}
    inherited = [a for a in previous if str(a.get("filename", "")).casefold() not in replaced]
    snapshot = [*deliverables, *revisions, *inherited]
    if len(snapshot) > MAX_DELIVERABLES:
        raise ValueError("Complete artifact snapshot exceeds the 8-file limit; no files were published.")
    normalized = normalize_deliverables(snapshot)
    names = [d.get("filename") for d in normalized]
    if (len(normalized) != len(snapshot) or any(not n for n in names)
            or len({n.casefold() for n in names}) != len(names)
            or any("content" not in d or "content_error" in d for d in normalized)):
        raise ValueError("Complete artifact snapshot requires distinct valid text files.")
    sources = {a["filename"].casefold(): a for a in previous}
    available = {a["seq"]: a for group in accepted.values() for a in group}
    available.update({a["seq"]: a for a in previous})
    for edit in edits:
        source = available[edit["artifact_seq"]]
        sources[source["filename"].casefold()] = source
    provenance = {}
    for name, source in sources.items():
        submission, relation = submissions[source["seq"]]
        event = by_seq[source["seq"]]
        if submission["hashes"].get(str(source["seq"])) != source["content_sha256"]:
            raise ValueError("Source does not match its immutable submission.")
        provenance[name] = {"artifact_seq": source["seq"], "event_hash": event.hash,
                            "content_sha256": source["content_sha256"], "relation": relation,
                            "submission_seq": submission["seq"], "submission_digest": submission["digest"]}
    return normalized, provenance


def select_artifact_versions(rt, task_id, items):
    """Remove only proven ancestors; retain sibling revisions and naming conflicts."""
    from .work import WorkConflict

    events = rt.log.read(task_id=task_id)
    by_seq = {e.seq: e for e in events}
    submissions = {e.payload["digest"]: e for e in events if e.type == "work.submitted"}
    selected, roots = {}, []
    for item in items:
        submission = item["submission"]
        artifacts = rt.work.artifacts(task_id, submission)
        if len(artifacts) != len(submission["artifact_seqs"]):
            raise WorkConflict("Artifact snapshot is incomplete.")
        for artifact in artifacts:
            if (not isinstance(artifact.get("content"), str)
                    or hashlib.sha256(artifact["content"].encode("utf-8")).hexdigest()
                    != submission["hashes"].get(str(artifact["seq"]))):
                raise WorkConflict("Artifact snapshot no longer matches its submission.")
            selected[artifact["seq"]] = artifact
            roots.append((artifact["seq"], submission))

    def source_of(seq, target):
        event = by_seq[seq]
        proof = event.payload.get("revision_source")
        if not isinstance(proof, dict):
            return None
        source_seq = proof.get("artifact_seq")
        source = by_seq.get(source_seq) if type(source_seq) is int else None
        prior = by_seq.get(proof.get("submission_seq")) if type(proof.get("submission_seq")) is int else None
        if (source is None or source.type != "deliverable.published" or source.seq >= event.seq
                or source.hash != proof.get("event_hash") or prior is None or prior.type != "work.submitted"
                or prior.seq >= event.seq or prior.payload["digest"] != proof.get("submission_digest")
                or source_seq not in prior.payload["artifact_seqs"]
                or source.payload.get("content_sha256") != proof.get("content_sha256")
                or prior.payload["hashes"].get(str(source_seq)) != proof.get("content_sha256")
                or (safe_filename(source.payload.get("filename")) or "").casefold()
                != (safe_filename(event.payload.get("filename")) or "").casefold()):
            return None
        if proof.get("relation") == "previous_attempt":
            latest = next((e for e in reversed(events) if e.type == "work.submitted" and e.seq < event.seq
                           and e.payload.get("work_item_id") == target["work_item_id"]
                           and e.payload.get("plan_seq") == target["plan_seq"]
                           and e.payload.get("scope") == target["scope"]), None)
            if latest != prior or prior.actor.id != event.actor.id:
                return None
        elif proof.get("relation") == "accepted_input":
            pending, seen = list(target.get("inputs", {}).values()), set()
            while pending:
                value = pending.pop()
                if value in seen:
                    continue
                seen.add(value)
                dependency = submissions.get(value)
                if (dependency is None or dependency.payload["plan_seq"] != target["plan_seq"]
                        or dependency.payload["scope"] != target["scope"]):
                    return None
                pending.extend(dependency.payload.get("inputs", {}).values())
            if prior.payload["digest"] not in seen:
                return None
        else:
            return None
        return source_seq, {**prior.payload, "seq": prior.seq}

    superseded = set()
    for seq, submission in roots:
        while (source := source_of(seq, submission)) is not None:
            seq, submission = source
            superseded.add(seq)
    return [artifact for seq, artifact in selected.items() if seq not in superseded]
