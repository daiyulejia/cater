"""Persistent task directories authorized by a host template, not OS sandboxes."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import os
import re
import uuid

from .events import Actor
from .workspace_tools import WorkspacePolicy, _directories


MANAGED_ID = "__managed__"


def parse_template(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or not {"root", "mode"} <= raw.keys():
        raise ValueError("managed requires an explicit root and mode")
    return WorkspacePolicy.from_dict(raw)


def database_namespace(log):
    if log.path == ":memory:":
        # Independent in-memory databases must never share task directories.
        if not hasattr(log, "_managed_workspace_namespace"):
            log._managed_workspace_namespace = uuid.uuid4().hex
        identity = "memory:" + log._managed_workspace_namespace
    else:
        identity = "file:" + os.path.normcase(os.path.realpath(log.path))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def managed_id(task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r"task_[a-z0-9_-]{1,59}", task_id):
        raise ValueError("managed workspaces require a task identifier")
    return MANAGED_ID + task_id


def task_policy(template, namespace, task_id, *, create=False):
    managed_id(task_id)
    policy = WorkspacePolicy.from_dict(template.as_dict())
    root = policy.root
    for part in (namespace, task_id):
        _directories(root)
        root = root / part
        if create:
            root.mkdir(mode=0o700, exist_ok=True)
        _directories(root)
    return WorkspacePolicy.from_dict({**policy.as_dict(), "root": str(root)})


class WorkspaceConfiguration(Mapping):
    """All executor lookups resolve the current host policy and durable binding."""

    def __init__(self, service):
        self.service = service

    def __getitem__(self, identity):
        service = self.service
        service._refresh()
        if identity in service._configured:
            return service._configured[identity]
        if isinstance(identity, str) and identity.startswith(MANAGED_ID) and service._managed is not None:
            task_id = identity[len(MANAGED_ID):]
            event = service._binding(task_id)
            if (event and event.payload.get("managed") is True
                    and event.payload.get("workspace_id") == identity):
                try:
                    row = service._managed_entry(task_id)
                except (OSError, ValueError):
                    pass
                else:
                    if (row["policy"].as_dict()["root"] == event.payload.get("root")
                            and row["fingerprint"] == event.payload.get("fingerprint")):
                        return row
        raise KeyError(identity)

    def __iter__(self):
        service = self.service
        service._refresh()
        identities = dict.fromkeys(service._configured)
        if service._managed is not None:
            for event in service.rt.log.iter_events(event_types=("workspace.bound",)):
                if event.actor == Actor.human() and event.payload.get("managed") is True:
                    identity = event.payload.get("workspace_id")
                    if isinstance(identity, str) and identity in self:
                        identities[identity] = None
        return iter(identities)

    def __len__(self):
        return sum(1 for _ in self)
