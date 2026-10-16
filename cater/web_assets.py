"""Bounded, process-local snapshots of the workroom's published files."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Mapping
from urllib.parse import unquote, urlsplit

from .artifact_store import _directories, _inspect, _name, _open


MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILES = 512
MAX_ENTRIES = 1024
MAX_DEPTH = 32
MANIFEST_NAME = "workroom-manifest.json"
MAX_MANIFEST_BYTES = 1024 * 1024


class WebAssetsError(ValueError):
    """The selected UI cannot be captured completely and consistently."""


@dataclass(frozen=True)
class WebAssets:
    files: Mapping[str, bytes]
    version: str


def _component(name: str) -> str:
    _name(name)
    if "%" in name or "#" in name:
        raise WebAssetsError("ambiguous UI asset filename")
    return name


def asset_path(path: str) -> str | None:
    """Decode each component once, without normalizing traversal or aliases."""
    if path == "/":
        return "/index.html"
    try:
        parts = [_component(unquote(part, errors="strict")) for part in path.split("/")[1:]]
    except ValueError:
        return None
    if not path.startswith("/"):
        return None
    if parts in (["index.html"], ["favicon.svg"]) or (len(parts) > 1 and parts[0] == "assets"):
        return "/" + "/".join(parts)
    return None


def _stamp(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, getattr(info, "st_file_attributes", 0))


def _ancestors(root: Path) -> tuple:
    # Outside the selected tree, unrelated sibling writes may change timestamps.
    return tuple((info.st_dev, info.st_ino, info.st_mode, getattr(info, "st_file_attributes", 0))
                 for info in (_inspect(parent, True) for parent in root.parents))


def _inventory(root: Path) -> dict[str, tuple]:
    _directories(root)
    entries = {"": _stamp(_inspect(root, True))}
    files = total = 0

    def add(path: Path, directory: bool) -> None:
        nonlocal files, total
        info = _inspect(path, directory)
        entries[path.relative_to(root).as_posix()] = _stamp(info)
        if len(entries) > MAX_ENTRIES:
            raise WebAssetsError("UI asset entry count exceeds limit")
        if not directory:
            files += 1
            total += info.st_size
            if files > MAX_FILES:
                raise WebAssetsError("UI asset file count exceeds limit")
            if info.st_size > MAX_FILE_BYTES:
                raise WebAssetsError("UI asset file exceeds byte limit")
            if path == root / MANIFEST_NAME and info.st_size > MAX_MANIFEST_BYTES:
                raise WebAssetsError("UI manifest exceeds byte limit")
            if total > MAX_TOTAL_BYTES:
                raise WebAssetsError("UI assets exceed total byte limit")

    add(root / "index.html", False)
    for name in ("favicon.svg", "assets", MANIFEST_NAME):
        path = root / name
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        add(path, name == "assets")
    pending = [root / "assets"] if "assets" in entries else []
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                _component(child.name)
                path = directory / child.name
                if len(path.relative_to(root).parts) > MAX_DEPTH:
                    raise WebAssetsError("UI asset directory depth exceeds limit")
                is_directory = stat.S_ISDIR(path.lstat().st_mode)
                add(path, is_directory)
                if is_directory:
                    pending.append(path)
    return entries


def _read_asset(path: Path, expected: tuple) -> bytes:
    with os.fdopen(_open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)), "rb") as stream:
        opened = _stamp(os.fstat(stream.fileno()))
        # Windows fstat reports birth time where lstat reports change time.
        if os.name == "nt":
            same_file = opened[:6] + opened[7:] == expected[:6] + expected[7:]
        else:
            same_file = opened == expected
        if not same_file:
            raise WebAssetsError("UI asset changed before reading")
        raw = stream.read(expected[4] + 1)
        if len(raw) != expected[4] or _stamp(os.fstat(stream.fileno())) != opened:
            raise WebAssetsError("UI asset changed while reading")
    if _stamp(_inspect(path, False)) != expected:
        raise WebAssetsError("UI asset changed after reading")
    return raw


class _EntryReferences(HTMLParser):
    def __init__(self, files: Mapping[str, bytes]):
        super().__init__(convert_charrefs=True)
        self.files = files
        self.scripts: set[str] = set()
        self.styles: set[str] = set()
        self.preloads: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"script", "link"} and len(values) != len(attrs):
            raise WebAssetsError("UI entry contains ambiguous resource attributes")
        if tag == "base":
            raise WebAssetsError("UI entry must not override the resource base URL")
        reference = None
        if tag == "script" and "src" in values:
            reference = values["src"] or ""
        elif tag == "link":
            relations = (values.get("rel") or "").lower().split()
            if set(relations) & {"stylesheet", "modulepreload", "icon"}:
                reference = values.get("href") or ""
        if reference is None:
            return
        parsed = urlsplit(reference)
        if parsed.scheme or parsed.netloc or reference.startswith("//") or not parsed.path:
            raise WebAssetsError("UI entry requires a local resource URL")
        path = parsed.path.removeprefix("./")
        key = asset_path(path if path.startswith("/") else "/" + path)
        if key is None or key not in self.files:
            raise WebAssetsError(f"UI entry references a missing or unsafe resource: {reference!r}")
        if tag == "script":
            self.scripts.add(key)
        elif "stylesheet" in relations:
            self.styles.add(key)
        elif "modulepreload" in relations:
            self.preloads.add(key)

    handle_startendtag = handle_starttag


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise WebAssetsError("UI manifest contains duplicate JSON fields")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise WebAssetsError("UI manifest contains a non-JSON constant")


def _validate_manifest(files: Mapping[str, bytes], entry: _EntryReferences) -> None:
    raw = files.get("/" + MANIFEST_NAME)
    if raw is None:
        if entry.scripts or entry.styles or entry.preloads:
            raise WebAssetsError("UI entry requires a build manifest")
        return
    if len(raw) > MAX_MANIFEST_BYTES:
        raise WebAssetsError("UI manifest exceeds byte limit")
    manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
    if not isinstance(manifest, dict) or len(manifest) > MAX_FILES:
        raise WebAssetsError("UI manifest must be a bounded object")

    def output(value: object) -> str:
        # Manifest output names are literal relative paths, not URLs or source keys.
        if not isinstance(value, str) or not value.startswith("assets/"):
            raise WebAssetsError("UI manifest output must stay within assets")
        parts = value.split("/")
        if len(parts) > MAX_DEPTH:
            raise WebAssetsError("UI manifest output depth exceeds limit")
        for part in parts:
            _component(part)
        key = "/" + value
        if key not in files:
            raise WebAssetsError(f"UI manifest references a missing resource: {value!r}")
        return key

    outputs: dict[str, str] = {}
    css: dict[str, set[str]] = {}
    imports: dict[str, list[str]] = {}
    dynamic: dict[str, list[str]] = {}
    allowed = {"file", "css", "assets", "imports", "dynamicImports", "src", "name", "isEntry", "isDynamicEntry"}
    for key, chunk in manifest.items():
        if not key or len(key) > 1024 or not isinstance(chunk, dict) or set(chunk) - allowed:
            raise WebAssetsError("UI manifest contains an invalid chunk")
        for field in ("src", "name"):
            if field in chunk and not isinstance(chunk[field], str):
                raise WebAssetsError("UI manifest metadata must be strings")
        for field in ("isEntry", "isDynamicEntry"):
            if field in chunk and not isinstance(chunk[field], bool):
                raise WebAssetsError("UI manifest entry flags must be boolean")
        outputs[key] = output(chunk.get("file"))
        lists = {}
        for field in ("css", "assets", "imports", "dynamicImports"):
            values = chunk.get(field, [])
            if not isinstance(values, list) or len(values) > MAX_FILES or any(not isinstance(v, str) for v in values):
                raise WebAssetsError("UI manifest dependencies must be bounded string arrays")
            lists[field] = values
        css[key] = {output(value) for value in lists["css"]}
        for value in lists["assets"]:
            output(value)
        imports[key], dynamic[key] = lists["imports"], lists["dynamicImports"]
        if any(value not in manifest for value in imports[key] + dynamic[key]):
            raise WebAssetsError("UI manifest references a missing chunk")

    roots = {key for key, chunk in manifest.items() if chunk.get("isEntry") is True
             and outputs[key] in entry.scripts | entry.styles}
    if entry.scripts - {outputs[key] for key in roots}:
        raise WebAssetsError("UI entry scripts do not match manifest entry chunks")
    if "index.html" in manifest and "index.html" not in roots:
        raise WebAssetsError("UI entry does not match the index.html manifest chunk")
    if "index.html" in manifest:
        roots = {"index.html"}
        if entry.scripts - {outputs["index.html"]}:
            raise WebAssetsError("UI entry scripts do not match the index.html manifest chunk")

    def reachable(include_dynamic: bool) -> set[str]:
        seen: set[str] = set()
        pending = list(roots)
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            pending.extend(value for value in imports[key] + (dynamic[key] if include_dynamic else []) if value not in seen)
        return seen

    static = reachable(False)
    styles = {value for key in static for value in css[key]}
    styles.update(outputs[key] for key in static if outputs[key].endswith(".css"))
    if entry.styles != styles:
        raise WebAssetsError("UI entry styles do not match manifest entry dependencies")
    if entry.preloads - {outputs[key] for key in reachable(True)}:
        raise WebAssetsError("UI entry preloads do not match manifest entry dependencies")


def capture_web_assets(root: str | Path) -> WebAssets:
    """Fail closed on observed changes; never retry into a different build.

    The host must protect the source and ancestors from hostile writers. Stat
    checks detect ordinary concurrent builds, not arbitrary filesystem ABA attacks.
    """
    selected = Path(root)
    if ".." in selected.parts:
        raise WebAssetsError("UI asset root must not contain parent traversal")
    selected = selected.absolute()  # Preserve links for inspection, never resolve them away.
    try:
        ancestors = _ancestors(selected)
        before = _inventory(selected)
        files = {"/" + name: _read_asset(selected / name, info)
                 for name, info in sorted(before.items()) if stat.S_ISREG(info[2])}
        if _inventory(selected) != before or _ancestors(selected) != ancestors:
            raise WebAssetsError("UI asset tree changed during capture")
        parser = _EntryReferences(files)
        parser.feed(files["/index.html"].decode("utf-8"))
        parser.close()
        _validate_manifest(files, parser)
    except WebAssetsError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise WebAssetsError(f"cannot capture UI assets: {exc}") from exc
    version = hashlib.sha256()
    for name, raw in sorted(files.items()):
        encoded = name.encode("utf-8")
        version.update(len(encoded).to_bytes(4, "big"))
        version.update(encoded)
        version.update(len(raw).to_bytes(8, "big"))
        version.update(raw)
    files.pop("/" + MANIFEST_NAME, None)
    return WebAssets(MappingProxyType(files), version.hexdigest())
