"""Structured API contracts proven by the original real download run.

Protocol reference (MIT): OpenListTeam/openlist-baidushare-plugin,
commit 8515b0f7bfa715007c833d1374fdb10b65fcc217. Public client constants
are parsed as data in memory; remote source is never executed or saved.
"""

from .errors import InputError

import base64
import hashlib
import hmac
import json
import re
import shutil
import subprocess
import time
from collections import deque
from urllib.parse import quote, urlsplit

import requests

REFERENCE = "OpenListTeam/openlist-baidushare-plugin"
REVISION = "8515b0f7bfa715007c833d1374fdb10b65fcc217"
UPLOAD_REFERENCE = "qjfoidnh/BaiduPCS-Go@d827497caf359671eb4d21fac07ec3ac09d889e0 (baidupcs/upload.go, baidupcs/prepare.go)"
UPLOAD_REVISION = "d827497caf359671eb4d21fac07ec3ac09d889e0"
VIDEO = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm", ".ts", ".flv", ".wmv", ".mpeg", ".mpg"}
IMAGE = {".png", ".jpg", ".jpeg", ".webp"}
SLICE = 4*1024*1024          # 4 MB upload slices (fixed for non-vip accounts)
SLICE_MD5_BYTES = 256*1024   # slice-md5 covers the first 256 KB of the file
PCS_APP_ID = "250528"        # public web app id used by the pinned BaiduPCS-Go client


class ProviderError(Exception):
    """Expose only sanitized stage/status information to callers."""

    def __init__(self, stage, status=None, code=None):
        self.stage, self.status, self.code = stage, status, code
        super().__init__(f"Provider request failed: stage={stage}, HTTP={status}, code={code}")


def check_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not any(host == root or host.endswith("."+root) for root in ("baidu.com", "baidupcs.com", "baidubce.com"))):
        raise InputError("Only HTTPS provider destinations on port 443 are allowed")


class ProviderSession(requests.Session):
    """Scope transport enforcement to this client, including every redirect."""

    def send(self, request, **kwargs):
        check_url(request.url)
        if kwargs.get("verify") is False:
            raise InputError("TLS verification must remain enabled")
        return super().send(request, **kwargs)

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", (10, 30))
        return super().request(method, url, **kwargs)


def session(bduss, stoken):
    client = ProviderSession()
    client.headers["User-Agent"] = "netdisk"
    client.cookies.set("BDUSS", bduss, domain=".baidu.com", path="/")
    client.cookies.set("STOKEN", stoken, domain=".baidu.com", path="/")
    return client


def checked(response, stage):
    if response.status_code >= 400:
        raise ProviderError(stage, status=response.status_code)
    try:
        data = response.json()
        key = "no" if stage == "account_sync" else "errno"
        code = int(data.get(key, -1))
    except (ValueError, AttributeError, TypeError):
        raise ProviderError(stage, status=response.status_code) from None
    if code:
        raise ProviderError(stage, status=response.status_code, code=code)
    return data


def retry(operation):
    for attempt in range(3):
        try:
            return operation()
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
            time.sleep(2*(attempt+1))


def protocol(encoded=None):
    if encoded is None:
        if not shutil.which("gh"):
            raise InputError("Install GitHub CLI (gh), or supply the pinned public reference using --protocol-stdin")
        result = subprocess.run(["gh", "api", f"repos/{REFERENCE}/contents/helper.go?ref={REVISION}", "--jq", ".content"],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise InputError("Public protocol fetch failed; check gh connectivity or use --protocol-stdin")
        encoded = result.stdout
    source = base64.b64decode(encoded).decode()
    values = {}
    for name in ("deviceID", "appSignatureCertMD5", "appVersion", "hmacDynamicKey"):
        match = re.search(r"\b"+name+r'\s*=\s*"([^\"]+)"', source)
        if not match:
            raise InputError("Pinned public protocol reference is incomplete")
        values[name] = match[1]
    return values


def rc4(data, key):
    """Decode the provider's existing session format, not local encryption."""
    state, j = list(range(256)), 0
    for i in range(256):
        j = (j+state[i]+key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    i = j = 0
    out = bytearray()
    for byte in data:
        i = (i+1) % 256
        j = (j+state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i]+state[j]) % 256])
    return bytes(out)


def account(client, constants):
    data = checked(retry(lambda: client.get("https://tieba.baidu.com/mo/q/sync")), "account_sync")
    uid = str(data.get("data", {}).get("user_id", ""))
    if not uid.isdigit():
        raise ProviderError("account_identity")
    data = checked(retry(lambda: client.get("https://pan.baidu.com/api/report/user", params={
        "action": "ANDROID_ACTIVE_BACKGROUND_UPLOAD_AND_DOWNLOAD", "clienttype": "1", "needrookie": "1",
        "timestamp": str(int(time.time())), "bind_uid": uid, "channel": "android"})), "account_session")
    try:
        key = rc4(base64.b64decode(data["uinfo"], validate=True), uid.encode())
    except (KeyError, ValueError):
        raise ProviderError("account_session_format") from None
    if not key:
        raise ProviderError("account_session_empty")
    return {"session": client, "uid": uid, "key": key, "protocol": constants}


def listing(client, url, code, directory=None, page=1):
    return checked(retry(lambda: client.post("https://pan.baidu.com/share/wxlist",
        params={"channel": "weixin", "version": "2.2.2", "clienttype": "25", "web": "1"},
        data={"pwd": code, "root": "1" if directory is None else "0", "dir": directory or "/",
              "shorturl": url.rsplit("/", 1)[-1], "num": "1000", "page": str(page), "order": "time"})), "share_list").get("data", {})


def scan_share(client, url, code):
    """Return all nested pages as plain metadata; never request download links."""
    from pathlib import PurePosixPath
    queue, seen, paths, entries = deque([None]), set(), set(), []
    while queue:
        directory, page = queue.popleft(), 1
        while True:
            data = listing(client, url, code, directory, page)
            batch = data.get("list")
            if not isinstance(batch, list):
                raise ProviderError("listing_shape")
            fresh = 0
            for item in batch:
                identifier, path = int(item["fs_id"]), item["path"]
                if identifier in seen or path in paths:
                    raise InputError("Repeated or conflicting share inventory entry")
                if not isinstance(path, str) or not path.startswith("/") or ".." in PurePosixPath(path).parts:
                    raise InputError("Invalid provider path")
                is_dir, size = bool(int(item["isdir"])), int(item["size"])
                if identifier <= 0 or size < 0:
                    raise InputError("Invalid provider file metadata")
                suffix = PurePosixPath(path).suffix.lower()
                kind = "directory" if is_dir else "video" if suffix in VIDEO else "image" if suffix in IMAGE else "other"
                entries.append({"path": path, "fs_id": identifier, "size": size, "kind": kind})
                fresh += 1
                seen.add(identifier)
                paths.add(path)
                if is_dir:
                    queue.append(path)
            if not data.get("more"):
                break
            if not fresh:
                raise InputError("Share pagination repeated without progress")
            page += 1
    return entries


def open_share(context, source, code):
    data = listing(context["session"], source["share_url"], code)
    if not all(data.get(key) for key in ("list", "seckey", "uk", "shareid")):
        raise ProviderError("share_authorization_shape")
    return {"uk": str(data["uk"]), "shareid": str(data["shareid"]),
            "sekey": data["seckey"].replace("-", "+").replace("~", "=").replace("_", "/")}


def get_link(context, share, entry):
    constants, client = context["protocol"], context["session"]
    stamp = int(time.time())
    milliseconds = stamp*1000
    bduss = client.cookies.get("BDUSS", domain=".baidu.com", path="/")
    material = (hashlib.sha1(bduss.encode()).hexdigest().encode()+context["uid"].encode()+context["key"]
                +str(milliseconds).encode()+constants["deviceID"].encode()+constants["appVersion"].encode()
                +constants["appSignatureCertMD5"].encode())
    extra = quote(json.dumps({"sekey": share["sekey"]}, separators=(",", ":")), safe="")
    body = f'encrypt=0&uk={share["uk"]}&product=share&primaryid={share["shareid"]}&fid_list=["{entry["fs_id"]}"]&extra={extra}'
    signature = hmac.new(constants["hmacDynamicKey"].encode(), f'{body}_{constants["deviceID"]}_{stamp}'.encode(), hashlib.sha1).hexdigest()
    data = checked(retry(lambda: client.post("https://pan.baidu.com/api/sharedownload", params={
        "sign": signature, "timestamp": str(stamp), "rand": hashlib.sha1(material).hexdigest(), "time": str(milliseconds),
        "devuid": constants["deviceID"], "channel": "android", "clienttype": "1", "version": constants["appVersion"]},
        data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})), "share_download")
    try:
        url = data["list"][0]["dlink"]
        if url.startswith("http://"):
            url = "https://"+url[7:]
        check_url(url)
    except (KeyError, IndexError, TypeError):
        raise ProviderError("download_link_shape") from None
    return url


def quota(client):
    """Return (total, used) bytes; the basis for pre-upload quota checks."""
    data = checked(retry(lambda: client.get("https://pan.baidu.com/api/quota",
        params={"checkfree": "1", "checkexpire": "1"})), "quota")
    try:
        total, used = int(data["total"]), int(data["used"])
    except (KeyError, TypeError, ValueError):
        raise ProviderError("quota_shape") from None
    if total < 0 or used < 0 or used > total:
        raise ProviderError("quota_shape")
    return total, used


def ensure_directory(client, path):
    """Create remote directories level by level; rtype=3 keeps existing ones."""
    parts = [part for part in path.split("/") if part]
    current = ""
    for part in parts:
        current += "/"+part
        checked(retry(lambda: client.post("https://pan.baidu.com/api/create",
            params={"a": "commit"},
            data={"path": current, "isdir": "1", "rtype": "3"},
            headers={"Content-Type": "application/x-www-form-urlencoded"})), "remote_mkdir")


def digest_blocks(path):
    """Content/block/slice digests in one streaming pass; sizes come from stat."""
    import os
    size = os.stat(path).st_size
    content, blocks = hashlib.md5(), []
    with open(path, "rb") as handle:
        remaining = SLICE_MD5_BYTES
        slice_md5 = hashlib.md5()
        while remaining > 0:
            head = handle.read(min(1024*1024, remaining))
            if not head:
                break
            slice_md5.update(head)
            remaining -= len(head)
        handle.seek(0)
        block = hashlib.md5()
        counted = 0
        while True:
            chunk = handle.read(4*1024*1024)
            if not chunk:
                break
            content.update(chunk)
            block.update(chunk)
            counted += len(chunk)
            if counted % SLICE == 0:
                blocks.append(block.hexdigest())
                block = hashlib.md5()
    if counted % SLICE or not blocks:
        blocks.append(block.hexdigest())
    return size, content.hexdigest(), blocks, slice_md5.hexdigest()


def precreate(client, path, digests, mtime):
    """Reserve an upload; return_type 2 means the server matched a known file."""
    size, content, blocks, slice_md5 = digests
    data = checked(retry(lambda: client.post("https://pan.baidu.com/api/precreate",
        data={"path": path, "size": str(size), "isdir": "0", "autoinit": "1", "rtype": "2",
              "block_list": json.dumps(blocks, separators=(",", ":")),
              "content-md5": content, "slice-md5": slice_md5,
              "local_mtime": str(mtime), "local_ctime": str(mtime)},
        headers={"Content-Type": "application/x-www-form-urlencoded"})), "upload_precreate")
    try:
        kind = int(data["return_type"])
        uploadid = str(data["uploadid"])
        pending = [int(value) for value in data["block_list"]]
    except (KeyError, TypeError, ValueError):
        raise ProviderError("upload_precreate_shape") from None
    if kind == 2:
        return {"rapid": True}
    if kind != 1 or not uploadid or any(seq not in range(len(blocks)) for seq in pending):
        raise ProviderError("upload_precreate_shape")
    return {"rapid": False, "uploadid": uploadid, "pending": pending}


def upload_slice(client, uploadid, local_path, remote_path, seq, total, digests):
    """Upload one 4 MB slice as multipart form data to the PCS endpoint."""
    size, _, blocks, _ = digests
    offset = seq*SLICE
    length = size-offset if seq == total-1 else SLICE
    boundary = "----baidudownloader"+hashlib.sha1(f"{remote_path}:{seq}".encode()).hexdigest()
    with open(local_path, "rb") as handle:
        handle.seek(offset)
        remaining = length
        hasher = hashlib.md5()

        def generate():
            nonlocal remaining
            yield (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"slice\"\r\n"
                   f"Content-Type: application/octet-stream\r\n\r\n").encode()
            while remaining > 0:
                chunk = handle.read(min(256*1024, remaining))
                if not chunk:
                    raise InputError("Local file shrank while uploading")
                remaining -= len(chunk)
                hasher.update(chunk)
                yield chunk
            yield f"\r\n--{boundary}--\r\n".encode()

        response = client.post("https://pcs.baidu.com/rest/2.0/pcs/superfile2",
            params={"method": "upload", "type": "tmpfile", "path": remote_path,
                    "uploadid": uploadid, "partseq": str(seq), "app_id": PCS_APP_ID},
            data=generate(), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    if response.status_code >= 400:
        raise ProviderError("upload_slice", status=response.status_code)
    try:
        data = response.json()
        if int(data.get("error_code", 0)) or int(data.get("errno", 0)):
            raise ProviderError("upload_slice", code=int(data.get("error_code") or data.get("errno")))
    except ValueError:
        raise ProviderError("upload_slice_shape") from None
    if hasher.hexdigest() != blocks[seq]:
        raise InputError("Uploaded slice digest differs from the plan")


def finish_create(client, path, digests, uploadid, mtime):
    """Merge uploaded slices into the final remote file; rtype=2 refuses overwrite."""
    size, _, blocks, _ = digests
    data = checked(retry(lambda: client.post("https://pan.baidu.com/api/create",
        data={"path": path, "size": str(size), "isdir": "0", "rtype": "2", "uploadid": uploadid,
              "block_list": json.dumps(blocks, separators=(",", ":")),
              "local_mtime": str(mtime), "local_ctime": str(mtime)},
        headers={"Content-Type": "application/x-www-form-urlencoded"})), "upload_create")
    if not data.get("fs_id") or not data.get("path"):
        raise ProviderError("upload_create_shape")
    return {"fs_id": int(data["fs_id"]), "remote_path": data["path"]}
