"""Command-line workflow and bounded execution; no task-specific paths."""

from .errors import InputError

import argparse
import csv
import getpass
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import __version__, api, model, transfer


def emit(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def credentials():
    bduss = (os.environ.get("BAIDU_BDUSS") or getpass.getpass("BDUSS (hidden): ")).strip().replace("\\~", "~")
    stoken = (os.environ.get("BAIDU_STOKEN") or getpass.getpass("STOKEN (hidden): ")).strip()
    if not bduss or not stoken or any(char in bduss+stoken for char in "\r\n"):
        raise InputError("Both account credentials are required without embedded newlines")
    return bduss, stoken


def extraction_codes(rows, supplied=None):
    codes = {}
    provided = {}
    if supplied:
        normalized, values = model.sources(model.read(supplied))
        provided = {row["share_url"]: values[row["id"]] for row in normalized if row["id"] in values}
    for row in rows:
        code = provided.get(row["share_url"], os.environ.get("BAIDU_SHARE_CODE"))
        if code is None:
            code = getpass.getpass(f"Extraction code for {row['id']} (blank if none, hidden): ")
        codes[row["id"]] = model.validate_code(code.strip())
    return codes


def scan(args):
    rows = model.read(args.sources) if args.sources else [{"title": args.title, "share_url": args.url}]
    normalized, supplied = model.sources(rows)
    if (args.job/"scan.json").exists():
        raise InputError("This job already has a scan; choose a new job directory to preserve its review/state")
    codes = {}
    for row in normalized:
        code = supplied.get(row["id"], os.environ.get("BAIDU_SHARE_CODE"))
        if code is None:
            code = getpass.getpass(f"Extraction code for {row['id']} (blank if none, hidden): ")
        codes[row["id"]] = model.validate_code(code.strip())
    client = api.session(*credentials())
    inventories, lines, selection = {}, ["# Share candidates", "", "Co-location is a candidate signal only. Names and numbering do not establish relevance.", ""], []
    try:
        for row in normalized:
            entries = api.scan_share(client, row["share_url"], codes[row["id"]])
            inventories[row["id"]] = entries
            lines += [f"## {row['id']}: {row['title']}", ""]
            for group in model.groups(entries):
                lines += [f"Group {group['group']} ({group['evidence']}): {group['directory']}", ""]
                lines += [f"- ID {entry['fs_id']}: {entry['path']} ({entry['size']:,} bytes)" for entry in group["files"]]
                lines.append("")
            images = [entry for entry in entries if entry["kind"] == "image"]
            lines += ["Image candidates (cover relevance requires review):", ""]
            lines += [f"- ID {entry['fs_id']}: {entry['path']} ({entry['size']:,} bytes)" for entry in images] or ["- None"]
            lines.append("")
            selection.append({"source_id": row["id"], "groups": [], "file_ids": [], "cover_ids": []})
            emit("scanned", source_id=row["id"], entries=len(entries), groups=len(model.groups(entries)), images=len(images))
    finally:
        client.close()
    model.write(args.job/"scan.json", {"schema": 1, "sources": normalized, "inventories": inventories})
    model.write(args.job/"selection.json", selection)
    (args.job/"review.md").write_text("\n".join(lines), encoding="utf-8")
    emit("review_required", review=str(args.job/"review.md"), selection=str(args.job/"selection.json"))


def prepare(args):
    plan = model.build_plan(model.read(args.job/"scan.json"), model.read(args.selection or args.job/"selection.json"), args.destination)
    model.validate_plan(plan)
    model.write(args.job/"plan.json", plan)
    lines = ["# Exact download plan", "", f"Plan: {plan['plan_id']}", f"Destination: {plan['destination']}",
             f"Files: {len(plan['files'])}; bytes: {plan['total_bytes']:,}", "", "Count anomalies:", ""]
    lines += [json.dumps(warning) for warning in plan["warnings"]] or ["None"]
    lines += ["", "Files:", ""]+[f"- {entry['kind']} | ID {entry['fs_id']} | {entry['relative_path']} | {entry['size']:,} bytes" for entry in plan["files"]]
    lines += ["", f"Confirm only this scope with: DOWNLOAD {plan['plan_id']}"]
    (args.job/"plan-review.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    emit("plan_ready", plan_id=plan["plan_id"], files=len(plan["files"]), bytes=plan["total_bytes"], warnings=plan["warnings"], review=str(args.job/"plan-review.md"))


def run_download(job, plan, codes, login, constants, workers):
    """Run a previously confirmed exact plan; progress counts newly received bytes."""
    model.validate_plan(plan)
    if not (job/"approval.json").exists() or model.read(job/"approval.json").get("plan_id") != plan["plan_id"]:
        raise InputError("A matching confirmed plan approval is required")
    if type(workers) is not int or not 1 <= workers <= 4:
        raise InputError("Worker count must be between 1 and 4")
    stop, mutex, local = threading.Event(), threading.Lock(), threading.local()
    clients, results, active = [], {}, {}
    started = time.monotonic()
    stats = {"new_bytes": 0, "last_write": started, "sample_time": started, "sample_bytes": 0}

    def snapshot(state="running", force=False):
        now = time.monotonic()
        if not force and now-stats["last_write"] < 1:
            return
        complete = [item for item in results.values() if item["state"] == "complete"]
        record = {"plan_id": plan["plan_id"], "state": state, "updated_at_epoch": time.time(), "pid": os.getpid(),
                  "planned_files": len(plan["files"]), "planned_bytes": plan["total_bytes"], "completed_files": len(complete),
                  "completed_bytes": sum(item["bytes"] for item in complete), "failed_files": sum(item["state"] == "failed" for item in results.values()),
                  "new_bytes": stats["new_bytes"], "elapsed_seconds": now-started,
                  "average_mb_s": stats["new_bytes"]/max(now-started, 0.001)/1e6, "active": dict(active), "results": dict(results)}
        model.write(job/"status.json", record)
        stats["last_write"] = now
        if force or now-stats["sample_time"] >= 5:
            rate = (stats["new_bytes"]-stats["sample_bytes"])/max(now-stats["sample_time"], 0.001)/1e6
            speed = job/"speed.csv"
            empty = not speed.exists()
            with speed.open("a", newline="") as output:
                writer = csv.writer(output)
                if empty:
                    writer.writerow(["run_started_epoch", "elapsed_seconds", "new_bytes", "interval_mb_s", "average_mb_s"])
                writer.writerow([run_epoch, round(now-started, 3), stats["new_bytes"], round(rate, 4), round(record["average_mb_s"], 4)])
            stats.update(sample_time=now, sample_bytes=stats["new_bytes"])
            emit("progress", state=state, completed=record["completed_files"], total=len(plan["files"]), new_bytes=stats["new_bytes"], interval_mb_s=round(rate, 3))

    source_map = {row["id"]: row for row in plan["sources"]}
    run_epoch = time.time()

    def worker(entry):
        if stop.is_set():
            return
        key = model.digest(entry)
        try:
            cached = transfer.existing(job, plan, entry)
            if cached:
                receipt = {**cached, "reused": True}
            else:
                if not hasattr(local, "context"):
                    client = api.session(*login)
                    with mutex:
                        clients.append(client)
                    local.context = api.account(client, constants)
                    local.shares = {}
                source_id = entry["source_id"]
                if source_id not in local.shares:
                    local.shares[source_id] = api.open_share(local.context, source_map[source_id], codes[source_id])
                if stop.is_set():
                    return

                def progress(delta, total):
                    if stop.is_set():
                        raise InterruptedError("Run stopped")
                    with mutex:
                        stats["new_bytes"] += delta
                        active[key] = {"file": entry["relative_path"], "received_bytes": total, "expected_bytes": entry["size"]}
                        snapshot()

                receipt = transfer.download(job, plan, entry, local.context, local.shares[source_id], progress)
            result = {"file": entry["relative_path"], "state": "complete", "bytes": entry["size"], "reused": receipt["reused"]}
        except Exception as error:
            if isinstance(error, api.ProviderError):
                stop.set()
            result = {"file": entry["relative_path"], "state": "failed", "error_type": type(error).__name__}
            if isinstance(error, api.ProviderError):
                result.update(stage=error.stage, http_status=error.status, provider_code=error.code)
            elif isinstance(error, InputError):
                result["message"] = str(error)
        with mutex:
            results[key] = result
            active.pop(key, None)
            snapshot(force=True)

    executor = ThreadPoolExecutor(max_workers=workers)
    interrupted = False
    try:
        with mutex:
            snapshot(force=True)
        futures = [executor.submit(worker, entry) for entry in plan["files"]]
        for future in as_completed(futures):
            future.result()
    except KeyboardInterrupt:
        interrupted = True
        stop.set()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        for client in clients:
            client.close()
        state = "interrupted" if interrupted else "complete" if len(results) == len(plan["files"]) and all(item["state"] == "complete" for item in results.values()) else "requires_attention"
        with mutex:
            snapshot(state=state, force=True)
    return 0 if state == "complete" else 1


def download(args):
    plan = model.validate_plan(model.read(args.job/"plan.json"))
    approval_path = args.job/"approval.json"
    approved = model.read(approval_path) if approval_path.exists() else {}
    if approved.get("plan_id") != plan["plan_id"]:
        phrase = "DOWNLOAD "+plan["plan_id"]
        emit("confirmation_required", phrase=phrase, files=len(plan["files"]), bytes=plan["total_bytes"], destination=plan["destination"], warnings=plan["warnings"])
        if (args.confirm if args.confirm is not None else input("Type the exact phrase after reviewing plan-review.md: ")) != phrase:
            raise InputError("Scope was not confirmed; nothing downloaded")
    pending = []
    remaining = 0
    for entry in plan["files"]:
        if not transfer.existing(args.job, plan, entry):
            pending.append(entry)
            _, partial, _ = transfer.paths(args.job, plan, entry)
            remaining += entry["size"]-(partial.stat().st_size if partial.exists() else 0)
    if any(entry["kind"] == "video" for entry in pending) and not shutil.which("ffprobe"):
        raise InputError("ffprobe is required before downloading videos")
    if not pending:
        model.write(approval_path, {"plan_id": plan["plan_id"], "confirmed_at_epoch": time.time()})
        return run_download(args.job, plan, {}, None, None, args.workers)
    ancestor = Path(plan["destination"])
    while not ancestor.exists():
        ancestor = ancestor.parent
    if shutil.disk_usage(ancestor).free < remaining+64*1024**2:
        raise InputError("Insufficient disk space for remaining bytes and reserve")
    # Refuse unsupported publication filesystems before transferring any media.
    with tempfile.TemporaryDirectory(dir=ancestor) as temporary:
        sample = Path(temporary)/"source"
        sample.touch()
        os.link(sample, Path(temporary)/"target")
    codes = extraction_codes(plan["sources"], args.sources)
    login = credentials()
    client = api.session(*login)
    try:
        for source in plan["sources"]:
            actual = {entry["fs_id"]: entry for entry in api.scan_share(client, source["share_url"], codes[source["id"]])}
            for entry in plan["files"]:
                if entry["source_id"] == source["id"] and actual.get(entry["fs_id"]) != {key: entry[key] for key in ("path", "fs_id", "size", "kind")}:
                    raise InputError("Source file changed; rescan and review a new plan")
    finally:
        client.close()
    constants = api.protocol(sys.stdin.read() if args.protocol_stdin else None)
    model.write(approval_path, {"plan_id": plan["plan_id"], "confirmed_at_epoch": time.time()})
    return run_download(args.job, plan, codes, login, constants, args.workers)


def verify(args):
    plan = model.validate_plan(model.read(args.job/"plan.json"))
    problems = []
    for entry in plan["files"]:
        try:
            if transfer.existing(args.job, plan, entry, deep=args.deep) is None:
                raise FileNotFoundError("Planned file is incomplete")
        except Exception as error:
            problems.append({"file": entry["relative_path"], "error_type": type(error).__name__})
    record = {"plan_id": plan["plan_id"], "state": "passed" if not problems else "incomplete", "files": len(plan["files"]), "deep": args.deep, "problems": problems}
    model.write(args.job/"verification.json", record)
    emit("verification", **record)
    return 1 if problems else 0


def parser():
    root = argparse.ArgumentParser(description="Review-first Baidu share video and cover downloader (macOS/Linux).")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check local prerequisites without authentication or network requests")
    item = commands.add_parser("scan", help="Enumerate video groups and image candidates without downloads")
    inputs = item.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--url")
    inputs.add_argument("--sources", type=Path)
    item.add_argument("--title")
    item.add_argument("--job", type=Path, required=True)
    item = commands.add_parser("plan", help="Bind explicitly selected groups/files to a destination")
    item.add_argument("--selection", type=Path)
    item.add_argument("--destination", type=Path, required=True)
    item.add_argument("--job", type=Path, required=True)
    item = commands.add_parser("download", help="Confirm exact scope, download or resume")
    item.add_argument("--job", type=Path, required=True)
    item.add_argument("--sources", type=Path, help="Original source input, used only to supply extraction codes in memory")
    item.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    item.add_argument("--confirm", help="Exact displayed DOWNLOAD phrase for a previously human-reviewed scope")
    item.add_argument("--protocol-stdin", action="store_true", help="Read base64-encoded pinned public helper.go from stdin")
    for command in ("status", "verify"):
        item = commands.add_parser(command)
        item.add_argument("--job", type=Path, required=True)
        if command == "verify":
            item.add_argument("--deep", action="store_true", help="Re-probe every video and fully decode/checksum every image")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            checks = {"python": sys.version.split()[0], "platform": sys.platform, "ffprobe": bool(shutil.which("ffprobe")), "gh": bool(shutil.which("gh")), "version": __version__}
            emit("doctor", **checks)
            return 0 if checks["ffprobe"] and sys.platform in ("darwin", "linux") else 1
        args.job = args.job.expanduser().resolve()
        if args.command == "status":
            state = model.read(args.job/"status.json")
            plan = model.validate_plan(model.read(args.job/"plan.json"))
            if state["plan_id"] != plan["plan_id"]:
                raise InputError("Saved status belongs to a different plan")
            emit("status", **{key: value for key, value in state.items() if key != "results"})
            return 0
        if args.command == "scan" and args.url and not args.title:
            raise InputError("--title is required for a single share")
        with model.locked(args.job):
            return {"scan": scan, "plan": prepare, "download": download, "verify": verify}[args.command](args) or 0
    except (Exception, KeyboardInterrupt) as error:
        info = {"error_type": type(error).__name__}
        if isinstance(error, api.ProviderError):
            info.update(stage=error.stage, http_status=error.status, provider_code=error.code)
        elif isinstance(error, InputError):
            info["message"] = str(error)
        emit("stopped", **info)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
