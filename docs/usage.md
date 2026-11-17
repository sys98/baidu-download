# Baidu share downloader

Review video groups and cover candidates from Baidu shares, then download only the exact files you approve. Supports single links and named JSON batches through structured APIs.

Requires Python 3.11+, macOS or Linux, ffprobe for videos, and a destination filesystem supporting hard links (including APFS and ext4). A same-directory hard-link check runs before transferring media. ExFAT/FAT destinations are not supported in this version. GitHub CLI (`gh`) supplies a pinned public client protocol reference; `--protocol-stdin` is an alternative. Provider client APIs may change; this tool does not guarantee availability or a particular speed.

Account credentials are entered through hidden prompts or existing process environment variables. They are never written to task files. Extraction codes are also excluded from generated plans/reports; enter them again when downloading, provide the original source list, or use the process environment. Existing browser cookies, credential files, and `.env` files are not read.

## Install and check

From the extracted project directory:

```sh
uv tool install .
baidu-download --help
baidu-download doctor
```

`doctor` is local only and makes no network requests. It reports `gh` separately because stdin can supply the public reference. It returns a failure exit code when ffprobe is missing, even though cover-only downloads do not need ffprobe. Install ffmpeg/ffprobe and GitHub CLI using your system package manager when required.

## Download one share

Read the URL and a local drama title interactively, then scan. Paste the share URL after the first `read` and enter the title after the second:

```sh
read -r SHARE_URL
read -r TITLE
baidu-download scan --url "$SHARE_URL" --title "$TITLE" --job ./job
```

Scanning lists the complete shared tree. It does not request download links, transfer files into your account, or download media. Open `job/review.md`, then edit `job/selection.json` to select groups and/or individual file IDs. The generated selection starts empty.

For example, after checking that group 1 is the intended video group for source 1, the selection is:

```json
[
  {"source_id": "1", "groups": [1], "file_ids": [], "cover_ids": []}
]
```

Copy any desired image IDs from the review into `cover_ids` to include covers. Multiple cover variants can be selected. Use `file_ids` for a specific video subset; leave `groups` empty when selecting only individual videos. Empty source selections are skipped. Missing covers are shown as `None` in the review; no replacement is generated.

Group membership depends on a direct parent directory. Filenames, numbering, sizes, or the largest group never establish relevance. Multiple groups and isolated videos remain visible. Unrelated videos in the same directory require human selection.

Create the exact plan and inspect `job/plan-review.md` before downloading:

```sh
baidu-download plan --job ./job --destination ./media
baidu-download download --job ./job --workers 4
```

The first download asks for the displayed `DOWNLOAD` phrase containing the plan hash. The phrase confirms exact IDs, source paths, byte counts, destination, and any count anomalies. Credentials are prompted after confirmation. The tool re-enumerates selected sources before transferring media and refuses changed file identities/sizes/types.

Completed files are saved under `media/<title>/<original filename>`. Same-title or same-filename collisions are rejected rather than silently renamed or overwritten. Existing unverified files are refused.

## Run a batch

Use a UTF-8 JSON array containing these fields for each share:

| Field | Required | Meaning |
| --- | --- | --- |
| `title` | Yes | Exact local drama directory name |
| `share_url` | Yes | HTTPS `pan.baidu.com/s/1...` URL; an input `pwd` query is accepted and removed from generated metadata |
| `id` | No | Unique ASCII ID; defaults to the row number |
| `expected_episodes` | No | Positive episode count for visible mismatch reporting |
| `extraction_code` | No | Input-only share code; hidden prompt if unavailable |

Additional source fields, such as `destination`, are ignored. This tool does not connect to spreadsheet services.

```sh
baidu-download scan --sources ./sources.json --job ./batch-job
```

Review `batch-job/review.md` and edit each source's selection in `batch-job/selection.json`, then:

```sh
baidu-download plan --job ./batch-job --destination ./media
baidu-download download --job ./batch-job --sources ./sources.json
```

`--sources` on download supplies extraction codes in memory using normalized share URLs. It cannot change the reviewed selection. A declared episode-count mismatch is displayed before confirmation, including when only a subset is selected. Confirmation accepts the explicitly listed files, not nonexistent episodes.

## Monitor, resume, and verify

```sh
baidu-download status --job ./job
baidu-download download --job ./job
baidu-download verify --job ./job
baidu-download verify --job ./job --deep
```

An unchanged approved plan resumes without another scope confirmation. Owned `.part` files resume only when the server returns the exact expected HTTP 206 byte range. Unrelated partial files are refused. Completed files with matching receipts, byte counts, and modification times are reused. A fully completed unchanged plan can be checked again without credentials or network requests.

During download, JSON progress includes received bytes and interval MB/s. `status` returns a compact snapshot; `status.json` also holds per-file current outcomes. `speed.csv` records the run start, elapsed time, newly transferred bytes, interval MB/s, and current-run average MB/s. MB/s uses 1,000,000 bytes. The run average includes request, retry, and validation time. Reused files and bytes already present in partials do not inflate the current run's transferred-byte count. CSV history is separated by run-start timestamp.

Default verification checks receipt identity, size, and modification time. `--deep` also runs ffprobe for every video and completely decodes/checksums every image. It checks planned files without deleting unrelated files in the destination.

## Authentication and recovery

Supported process environment variables are `BAIDU_BDUSS`, `BAIDU_STOKEN`, and optional `BAIDU_SHARE_CODE`. Missing values use hidden prompts. Supply environment variables through your existing process/secrets setup; avoid putting credentials in shell history. No account credential flags or automatic credential-file loading are provided.

For agent integration, `download --confirm` accepts the exact displayed phrase **after the user has approved that plan**. It is not an automatic approval flag. Approval records are local workflow state, not cryptographic proof of a human's identity.

If public protocol fetching through a subprocess fails, use the proven stdin path:

```sh
gh api 'repos/OpenListTeam/openlist-baidushare-plugin/contents/helper.go?ref=8515b0f7bfa715007c833d1374fdb10b65fcc217' --jq .content |
  baidu-download download --job ./job --protocol-stdin
```

Only the pinned reference is parsed as data in memory. The remote source is not executed or saved. Prompts use the terminal; avoid combining `--protocol-stdin` with commands that consume the same stdin for unrelated input. `gh` must already be usable if this fetch path is chosen.

Transient connection/TLS errors retry up to three attempts, keeping TLS verification enabled. Provider errors stop new work; an authentication/challenge/rate-limit problem is not described as a successful download. Resolve the provider issue, then rerun the same download command. The tool does not solve CAPTCHAs automatically or switch to a browser workflow.

If source identities/sizes changed, use a new job directory to scan and approve a new plan. Do not delete unrelated existing files or edit hashes to force approval. Changing selection or destination requires a new plan and confirmation. Disk preflight counts remaining bytes rather than all historically completed files. Only one mutating command can hold a job lock at a time.

Exit code 0 means the requested command passed; 1 means incomplete work or an actionable failure; argparse uses 2 for invalid command syntax. Sanitized per-file failures remain in `status.json`.

## Use from Python and run tests

Public functions accept ordinary dictionaries and caller-owned sessions. Import `api.session`, `api.scan_share`, `model.groups`, `model.build_plan`, and `model.validate_plan` for discovery and planning. `cli.run_download` is the execution primitive for an **already human-confirmed** plan; callers must enforce that approval and recheck live source metadata, as `cli.download` does. Use the CLI when those responsibilities should remain inside the tool.

```sh
uv run python -m unittest discover -s tests -v
```

Tests use temporary directories and mocked provider responses, including an offline CLI workflow. No real account is needed. A newly packaged live-provider run is not claimed.

Protocol reference: [OpenList Baidu share plugin, pinned commit](https://github.com/OpenListTeam/openlist-baidushare-plugin/tree/8515b0f7bfa715007c833d1374fdb10b65fcc217). Its MIT notice is retained in `THIRD-PARTY-LICENSE.txt`. The fixed contracts preserve account sync's `no` status, the short URL's leading `1`, and form encoding of the share authorization JSON before signing. No fundrive-baidu SDK installation or local SDK patch is required.
