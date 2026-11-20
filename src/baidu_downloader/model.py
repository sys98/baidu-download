"""Portable metadata, exact selection, and atomic job records."""

from .errors import InputError

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:20]


def name(value):
    if (not isinstance(value, str) or value in ("", ".", "..") or value != value.strip()
            or any(char in value for char in "/\\:\x00\n\r")):
        raise InputError("Unsafe or empty title/filename")
    return value


def collision_key(value):
    return unicodedata.normalize("NFC", value).casefold()


def sources(rows):
    """Normalize source input and separate extraction codes from saved metadata."""
    if not isinstance(rows, list) or not rows:
        raise InputError("Sources must be a nonempty JSON list")
    result, codes, ids, titles, urls = [], {}, set(), set(), set()
    for index, row in enumerate(rows, 1):
        url = urlsplit(row["share_url"])
        if (url.scheme != "https" or url.netloc != "pan.baidu.com" or not re.fullmatch(r"/s/1[A-Za-z0-9_-]+", url.path)
                or url.fragment or set(parse_qs(url.query))-{"pwd"}):
            raise InputError("Expected an HTTPS pan.baidu.com share URL")
        identifier, title = str(row.get("id", index)), name(row["title"])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
            raise InputError("Source IDs must be ASCII letters, numbers, underscores or hyphens")
        normalized = "https://pan.baidu.com"+url.path
        if identifier in ids or collision_key(title) in titles or normalized in urls:
            raise InputError("Duplicate source ID, title or share URL")
        expected = row.get("expected_episodes")
        if expected is not None and (type(expected) is not int or expected <= 0):
            raise InputError("Expected episodes must be a positive integer")
        record = {"id": identifier, "title": title, "share_url": normalized, "expected_episodes": expected}
        result.append(record)
        if "extraction_code" in row or "pwd" in parse_qs(url.query):
            codes[identifier] = validate_code(row.get("extraction_code", parse_qs(url.query).get("pwd", [""])[0]))
        ids.add(identifier)
        titles.add(collision_key(title))
        urls.add(normalized)
    return result, codes


def validate_code(code):
    if not isinstance(code, str) or (code and not re.fullmatch(r"[A-Za-z0-9]{4}", code)):
        raise InputError("Extraction codes must be blank or four ASCII letters/numbers")
    return code


def groups(entries):
    grouped = defaultdict(list)
    for entry in entries:
        if entry["kind"] == "video":
            grouped[str(PurePosixPath(entry["path"]).parent)].append(entry)
    return [{"group": index, "directory": directory, "files": sorted(files, key=lambda item: item["path"]),
             "evidence": "co-location" if len(files) >= 2 else "isolated-video", "content_verified": False}
            for index, (directory, files) in enumerate(sorted(grouped.items()), 1)]


def build_plan(scan, selection, destination):
    """Select enumerated IDs only; names and numbering never imply relevance."""
    if not isinstance(selection, list) or not selection:
        raise InputError("Selection must be a nonempty JSON list")
    by_id = {row["id"]: row for row in scan["sources"]}
    files, chosen, warnings, targets, selected_sources = [], set(), [], set(), []
    for item in selection:
        source_id = str(item["source_id"])
        if source_id in chosen or source_id not in by_id:
            raise InputError("Selection contains an unknown or repeated source ID")
        chosen.add(source_id)
        row = by_id[source_id]
        entries = scan["inventories"][source_id]
        lookup = {entry["fs_id"]: entry for entry in entries}
        selected = set()
        available_groups = {group["group"]: group for group in groups(entries)}
        for number in item.get("groups", []):
            if type(number) is not int or number not in available_groups:
                raise InputError("Unknown group number")
            selected.update(entry["fs_id"] for entry in available_groups[number]["files"])
        for key, kind in (("file_ids", "video"), ("cover_ids", "image")):
            for identifier in item.get(key, []):
                if type(identifier) is not int or identifier not in lookup or lookup[identifier]["kind"] != kind:
                    raise InputError("Selected file ID has the wrong type or was not scanned")
                selected.add(identifier)
        if not selected:
            continue
        selected_sources.append(row)
        video_count = sum(lookup[identifier]["kind"] == "video" for identifier in selected)
        if row["expected_episodes"] is not None and video_count != row["expected_episodes"]:
            warnings.append({"source_id": source_id, "expected_episodes": row["expected_episodes"], "selected_videos": video_count})
        for identifier in sorted(selected):
            entry = lookup[identifier]
            if type(entry["size"]) is not int or entry["size"] <= 0:
                raise InputError("Selected files require positive source byte counts")
            relative = row["title"]+"/"+name(PurePosixPath(entry["path"]).name)
            if collision_key(relative) in targets:
                raise InputError("Selected files collide in the destination; select a nonconflicting subset")
            targets.add(collision_key(relative))
            files.append({"source_id": source_id, **entry, "relative_path": relative})
    if not files:
        raise InputError("Select at least one video or cover")
    core = {"schema": 1, "sources": selected_sources, "files": files, "destination": str(Path(destination).expanduser().resolve())}
    return {**core, "plan_id": digest(core), "total_bytes": sum(entry["size"] for entry in files), "warnings": warnings}


def validate_plan(plan):
    if plan.get("schema") != 1:
        raise InputError("Unsupported plan version")
    core = {key: plan[key] for key in ("schema", "sources", "files", "destination")}
    if digest(core) != plan["plan_id"] or sum(entry["size"] for entry in plan["files"]) != plan["total_bytes"]:
        raise InputError("Plan changed; create and review a new plan")
    normalized, _ = sources(plan["sources"])
    if normalized != plan["sources"]:
        raise InputError("Plan source metadata is not normalized")
    root = Path(plan["destination"])
    if not root.is_absolute() or root.resolve() != root:
        raise InputError("Plan destination changed or contains a symlink")
    seen = set()
    source_ids = {row["id"]: row for row in plan["sources"]}
    for entry in plan["files"]:
        if entry["kind"] not in ("video", "image") or type(entry["fs_id"]) is not int or entry["fs_id"] <= 0 or type(entry["size"]) is not int or entry["size"] <= 0:
            raise InputError("Invalid planned file")
        row = source_ids[entry["source_id"]]
        expected = name(row["title"])+"/"+name(PurePosixPath(entry["path"]).name)
        target = root/entry["relative_path"]
        if entry["relative_path"] != expected or not target.resolve().is_relative_to(root) or target.is_symlink() or target.parent.is_symlink():
            raise InputError("Planned path escaped or changed")
        key = collision_key(entry["relative_path"])
        if key in seen:
            raise InputError("Conflicting planned targets")
        seen.add(key)
    if not seen:
        raise InputError("Empty plan")
    warnings = []
    for source_id, row in source_ids.items():
        count = sum(entry["source_id"] == source_id and entry["kind"] == "video" for entry in plan["files"])
        if row["expected_episodes"] is not None and count != row["expected_episodes"]:
            warnings.append({"source_id": source_id, "expected_episodes": row["expected_episodes"], "selected_videos": count})
    if warnings != plan["warnings"]:
        raise InputError("Plan count warnings changed; recreate the plan")
    return plan


@contextmanager
def locked(job):
    """A kernel lock survives interrupts without leaving a stale lock owner."""
    import fcntl
    job.mkdir(parents=True, exist_ok=True)
    with (job/".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InputError("Another command is modifying this job") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def remote_directory(value):
    """Remote upload roots must be absolute, normalized, and symlink-free."""
    if not isinstance(value, str) or not value.startswith("/") or value != value.strip() or "\x00" in value:
        raise InputError("Remote directory must be an absolute path")
    parts = [part for part in value.split("/") if part]
    if any(part in ("", ".", "..") for part in value.split("/")[1:]):
        raise InputError("Remote directory contains empty or relative segments")
    return "/"+"/".join(parts)


def build_upload_plan(roots, directory, files):
    """Enumerate concrete local files and map them onto the remote directory."""
    root = remote_directory(directory)
    entries, targets, seen_sources = [], set(), set()
    for raw in roots:
        source = Path(raw).expanduser()
        if source.is_symlink():
            raise InputError(f"Refusing symlink upload source: {source}")
        origin = source.resolve()
        if not origin.exists():
            raise InputError(f"Upload path does not exist: {origin}")
        candidates = sorted(origin.rglob("*")) if origin.is_dir() else [origin]
        batch = [item for item in candidates if item.is_file()]
        if not batch:
            raise InputError(f"No files found under: {origin}")
        for item in batch:
            if item.is_symlink():
                raise InputError(f"Refusing symlink upload source: {item}")
            try:
                relative = item.relative_to(origin) if origin.is_dir() else Path(item.name)
            except ValueError:
                raise InputError("Upload path escaped its root") from None
            remote = "/".join((root, *[name(part) for part in relative.parts]))
            key = collision_key(remote)
            if key in targets:
                raise InputError("Upload files collide on the same remote path")
            targets.add(key)
            stat = item.stat()
            if stat.st_size < 0:
                raise InputError("Invalid local file size")
            entries.append({"local_path": str(item), "remote_path": remote,
                            "size": stat.st_size, "mtime": int(stat.st_mtime)})
        seen_sources.add(str(origin))
    if not entries:
        raise InputError("Select at least one file to upload")
    core = {"schema": 1, "kind": "upload", "directory": root, "files": sorted(entries, key=lambda row: row["remote_path"])}
    return {**core, "plan_id": digest(core), "total_bytes": sum(row["size"] for row in core["files"])}


def validate_upload_plan(plan):
    if plan.get("schema") != 1 or plan.get("kind") != "upload":
        raise InputError("Unsupported upload plan version")
    core = {key: plan[key] for key in ("schema", "kind", "directory", "files")}
    if digest(core) != plan["plan_id"] or sum(row["size"] for row in plan["files"]) != plan["total_bytes"]:
        raise InputError("Upload plan changed; create and review a new plan")
    root = remote_directory(plan["directory"])
    root_parts = PurePosixPath(root).parts
    seen = set()
    for row in plan["files"]:
        if type(row["size"]) is not int or row["size"] < 0 or type(row["mtime"]) is not int:
            raise InputError("Invalid planned upload")
        parts = PurePosixPath(row["remote_path"]).parts
        if len(parts) <= len(root_parts) or parts[:len(root_parts)] != root_parts or any(part in ("", ".", "..") for part in parts):
            raise InputError("Planned remote path escaped or changed")
        expected = "/".join((root, *[name(part) for part in parts[len(root_parts):]]))
        key = collision_key(row["remote_path"])
        if key in seen:
            raise InputError("Conflicting planned upload targets")
        seen.add(key)
    if not seen:
        raise InputError("Empty upload plan")
    return plan
