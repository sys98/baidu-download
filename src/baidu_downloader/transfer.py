"""Owned partials, exact byte ranges, and media verification."""

from .errors import InputError

import hashlib
import json
import subprocess
import time
from pathlib import Path

from PIL import Image

from . import api, model


def paths(job, plan, entry):
    target = Path(plan["destination"])/entry["relative_path"]
    marker = job/"receipts"/plan["plan_id"]/(model.digest(entry)+".json")
    return target, target.with_name(target.name+".part"), marker


def identity(plan, entry):
    return {"plan_id": plan["plan_id"], "destination": plan["destination"], "file": entry}


def probe(path, kind):
    if kind == "image":
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            return {"kind": kind, "format": image.format, "width": image.width, "height": image.height,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height", "-of", "json", str(path)],
                            capture_output=True, text=True, timeout=60)
    data = json.loads(result.stdout) if result.returncode == 0 else {}
    if not any(item.get("codec_type") == "video" for item in data.get("streams", [])):
        raise InputError("Downloaded file has no verified video stream")
    return {"kind": kind, "media": data}


def existing(job, plan, entry, deep=False):
    target, partial, marker = paths(job, plan, entry)
    if target.is_symlink() or partial.is_symlink():
        raise InputError("Refusing symlink media or partial files")
    saved = model.read(marker) if marker.exists() else {}
    if target.exists():
        stat = target.stat()
        if (saved.get("identity") == identity(plan, entry) and saved.get("state") == "verified_partial"
                and stat.st_size == entry["size"] and stat.st_mtime_ns == saved.get("mtime_ns")
                and probe(target, entry["kind"]) == saved.get("probe")):
            if partial.exists():
                if not partial.samefile(target):
                    raise InputError("Interrupted publication has conflicting files")
                partial.unlink()
            saved["state"] = "complete"
            model.write(marker, saved)
        if (saved.get("identity") != identity(plan, entry) or saved.get("state") != "complete"
                or stat.st_size != entry["size"] or stat.st_mtime_ns != saved.get("mtime_ns")):
            raise FileExistsError("Existing file has no matching verification evidence")
        if deep and probe(target, entry["kind"]) != saved["probe"]:
            raise InputError("Existing media no longer matches its receipt")
        return saved
    if partial.exists() and (saved.get("identity") != identity(plan, entry) or partial.stat().st_size > entry["size"]):
        raise InputError("Unrelated or oversized partial file")
    return None


def receive(client, url, partial, expected_size, progress):
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > expected_size:
        raise InputError("Partial exceeds expected byte count")
    if offset == expected_size:
        return
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    with client.get(url, headers=headers, stream=True, timeout=(10, 60)) as response:
        if response.status_code >= 400:
            raise api.ProviderError("media_download", status=response.status_code)
        if offset:
            if response.status_code != 206 or response.headers.get("Content-Range") != f"bytes {offset}-{expected_size-1}/{expected_size}":
                raise InputError("Server did not honor the exact resume range")
        elif response.status_code != 200:
            raise InputError("Unexpected initial download response")
        length = response.headers.get("Content-Length")
        if length is not None and int(length) != expected_size-offset:
            raise InputError("Download response length differs from the plan")
        total = offset
        with partial.open("ab" if offset else "xb") as output:
            for chunk in response.iter_content(256*1024):
                if not chunk:
                    continue
                if total+len(chunk) > expected_size:
                    raise InputError("Provider returned too many bytes")
                output.write(chunk)
                total += len(chunk)
                progress(len(chunk), total)
        if total != expected_size or partial.stat().st_size != expected_size:
            raise InputError("Incomplete media byte count")


def download(job, plan, entry, context, share, progress):
    cached = existing(job, plan, entry)
    if cached:
        return {**cached, "reused": True}
    target, partial, marker = paths(job, plan, entry)
    target.parent.mkdir(parents=True, exist_ok=True)
    model.write(marker, {"identity": identity(plan, entry), "state": "partial"})
    started = time.monotonic()
    transferred = 0

    def update(delta, total):
        nonlocal transferred
        transferred += delta
        progress(delta, total)

    def attempt():
        if partial.exists() and partial.stat().st_size == entry["size"]:
            return
        url = api.get_link(context, share, entry)
        receive(context["session"], url, partial, entry["size"], update)

    api.retry(attempt)
    media = probe(partial, entry["kind"])
    if target.exists() or target.is_symlink():
        raise FileExistsError("Destination appeared while downloading")
    elapsed = time.monotonic()-started
    receipt = {"identity": identity(plan, entry), "state": "verified_partial", "bytes": entry["size"],
               "mtime_ns": partial.stat().st_mtime_ns, "probe": media, "transferred_bytes": transferred,
               "elapsed_seconds": elapsed, "average_mb_s": transferred/elapsed/1e6, "reused": False}
    model.write(marker, receipt)
    # Hard-link publication is atomic and never replaces another writer's file.
    import os
    os.link(partial, target)
    partial.unlink()
    receipt["state"] = "complete"
    model.write(marker, receipt)
    return receipt
