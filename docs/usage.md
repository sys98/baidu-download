# 百度网盘下载工具使用指南

先查看分享中的视频组和封面候选，再下载人工确认的文件。通过结构化 API 支持单链接和带剧名的 JSON 批量任务。

需要 Python 3.11+、macOS 或 Linux，以及支持硬链接的下载目录，例如 APFS、ext4。视频校验需要 ffprobe。开始传输前，会在目标目录所在文件系统中检查硬链接能力；当前版本不支持 ExFAT/FAT。GitHub CLI（`gh`）用于读取固定版本的公开客户端协议参考资料，也可用 `--protocol-stdin` 提供资料。网盘接口可能变化，工具不保证接口持续可用或固定下载速度。

账号凭证通过隐藏输入或已有的进程环境变量提供，不写入任务文件。提取码也不会写入生成的计划和报告：下载时可重新输入、提供原始来源清单，或通过进程环境提供。工具不读取浏览器 Cookie、账号凭据文件或 `.env` 文件。

## 安装与检查

在项目根目录执行：

```sh
uv tool install .
baidu-download --help
baidu-download doctor
```

`doctor` 只检查本机环境，不发起网络请求。它单独报告 `gh` 的可用性，因为协议资料也可以通过标准输入提供。即使仅下载封面不需要 ffprobe，缺少 ffprobe 时 `doctor` 仍会返回失败状态。请按需通过系统包管理器安装 ffmpeg/ffprobe 和 GitHub CLI。

## 下载一个分享链接

执行第一个 `read` 后粘贴分享链接，执行第二个 `read` 后输入本地剧名，再开始扫描：

```sh
read -r SHARE_URL
read -r TITLE
baidu-download scan --url "$SHARE_URL" --title "$TITLE" --job ./job
```

扫描会列出完整分享目录，不请求下载链接、不转存到自己的网盘，也不下载媒体文件。打开 `job/review.md`，然后编辑 `job/selection.json`，选择目录组或具体文件 ID。初始选择为空。

例如，确认来源 1 中的第 1 组为目标视频后，可填写：

```json
[
  {"source_id": "1", "groups": [1], "file_ids": [], "cover_ids": []}
]
```

需要封面时，把候选清单中的图片 ID 填入 `cover_ids`，支持选择多个版本。只下载指定视频时，把 ID 填入 `file_ids` 并将 `groups` 留空。没有选中文件的来源会跳过。没有图片候选时，清单显示 `None`，工具不会生成替代封面。

组按照文件的直接父目录划分。文件名、编号、大小和组内文件数量都不能证明内容相关。多个候选组和孤立视频会完整展示；同一目录中存在无关视频时，必须由人工选择。

生成具体计划，查看 `job/plan-review.md` 后再下载：

```sh
baidu-download plan --job ./job --destination ./media
baidu-download download --job ./job --workers 4
```

首次下载需要输入包含计划哈希的 `DOWNLOAD` 确认短语。批准范围包含具体文件 ID、源路径、字节数、目标目录和集数异常。确认后才提示输入账号凭证。传输前会重新枚举所选来源，拒绝身份、大小或类型发生变化的文件。

完整文件保存在 `media/<剧名>/<原文件名>`。剧名或文件名冲突时会拒绝执行，不擅自重命名或覆盖。已有文件缺少匹配校验记录时，也不会覆盖。

## 批量下载

输入文件使用 UTF-8 编码的 JSON 数组，每条分享包含以下字段：

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `title` | 是 | 本地剧名目录，按原文保留 |
| `share_url` | 是 | HTTPS `pan.baidu.com/s/1...` 分享链接；可带 `pwd` 参数，生成元数据时会移除该参数 |
| `id` | 否 | 唯一 ASCII 标识，默认使用行号 |
| `expected_episodes` | 否 | 声明集数，必须为正整数，用于明确显示数量差异 |
| `extraction_code` | 否 | 仅用于输入的提取码，缺失时隐藏提示输入 |

`destination` 等额外来源字段会被忽略。工具不直接连接电子表格服务。

```sh
baidu-download scan --sources ./sources.json --job ./batch-job
```

查看 `batch-job/review.md`，为各来源编辑 `batch-job/selection.json`，然后执行：

```sh
baidu-download plan --job ./batch-job --destination ./media
baidu-download download --job ./batch-job --sources ./sources.json
```

下载时的 `--sources` 只根据标准化分享链接在内存中提供提取码，不能更改已经审阅的选择。声明集数与选中文件数不一致时，即使只选择了部分视频，也会在确认前显示。确认仅批准清单中列出的文件，不代表缺失集数已经存在。

## 监控、续传与校验

```sh
baidu-download status --job ./job
baidu-download download --job ./job
baidu-download verify --job ./job
baidu-download verify --job ./job --deep
```

已经批准且范围未变的计划，可直接续跑，无需再次确认范围。归属明确的 `.part` 文件，只在服务端返回完全匹配的 HTTP 206 字节范围时续传；不明来源的临时文件会被拒绝。完整文件的校验记录、大小和修改时间全部匹配时，才会复用。全部完成且未改变的计划，再次执行时不需要凭证或网络请求。

下载过程输出 JSON 进度，包括接收字节数和分段 MB/s。`status` 返回简洁快照，`status.json` 还保留每个文件的当前结果。`speed.csv` 记录本轮开始时间、已用时间、新传输字节数、分段速度和本轮平均速度。1 MB 为 1,000,000 字节，平均速度包含请求、重试和校验耗时。复用文件及续传前已有的字节，不计入本轮新传输量。多轮 CSV 历史记录可通过各轮开始时间区分。

默认校验检查文件归属、大小、修改时间和已保存记录。`--deep` 还会对每个视频运行 ffprobe，并对每张图片完整解码、核对哈希；不包含视频逐帧完整解码。校验只针对计划中的文件，不删除目标目录中的其他文件。

## 凭证与故障恢复

支持的进程环境变量为 `BAIDU_BDUSS`、`BAIDU_STOKEN`，以及可选的 `BAIDU_SHARE_CODE`。缺失时使用隐藏输入提示。请通过已有的进程或凭证管理方式注入环境，避免将凭证写入命令历史。工具不提供账号凭证命令行参数，也不自动加载凭据文件。

供 agent 或其他脚本调用时，只有在**用户已经批准该计划**后，才能通过 `download --confirm` 提供工具显示的完整确认短语。它不是自动批准选项。批准记录属于本地工作流状态，不是能够证明人工身份的密码学凭据。

如果子进程获取公开协议资料失败，可以使用已验证的标准输入方式：

```sh
gh api 'repos/OpenListTeam/openlist-baidushare-plugin/contents/helper.go?ref=8515b0f7bfa715007c833d1374fdb10b65fcc217' --jq .content |
  baidu-download download --job ./job --protocol-stdin
```

只在内存中解析固定版本的参考数据，不执行、不保存远程源码。交互提示从终端读取；不要同时将同一标准输入用于其他交互命令。选择该方式时，`gh` 需要已经能够正常使用。

临时连接或 TLS 故障最多尝试 3 次，始终启用证书校验。网盘服务返回异常后，会停止新任务；认证、验证码或限流问题不会被报告为下载成功。处理服务端问题后，可重新执行原下载命令。工具不会自动识别验证码，也不会切换到浏览器转存流程。

源文件身份或大小变化时，请新建任务目录重新扫描和批准。不要删除无关已有文件，也不要修改哈希强行通过确认。改变选择范围或目标目录需要重新生成计划并确认。磁盘预检只计算剩余字节，不要求重复预留全部已完成文件的空间。同一任务目录同时只允许一个修改操作持有锁。

退出码 `0` 表示本次命令成功，`1` 表示存在未完成工作或错误；命令语法不正确时由 argparse 返回 `2`。脱敏后的逐文件失败信息保存在 `status.json`。

## 上传到指定目录

上传使用与下载一致的审阅流程，并按需求在两个时点校验额度。

```sh
baidu-download upload-plan --paths ./media/剧名 --remote-dir /来自下载工具/剧名 --job ./upjob
# 打开 upjob/plan-review.md：确切文件清单、字节总数、当前额度余量。
baidu-download upload --job ./upjob
```

- `upload-plan` 枚举本地文件（目录会递归展开，保留相对结构），生成上传计划并即时查询一次额度；余量不足会直接写入审阅文件，提示 `INSUFFICIENT`。
- `upload` 需要输入显示的 `UPLOAD <plan_id>` 确认短语，然后**再次**查询额度，`free < remaining` 时拒绝启动、不发起任何上传。
- 远程目录不存在时逐级创建；同名远程文件不会被覆盖（`rtype=2` 拒绝覆盖语义，服务端返回冲突错误时如实报告）。
- 文件按 4 MB 分片并行上传（最多 4 线程），逐片校验 MD5，完成后由服务端按分片列表合并，收据记录 `fs_id`。服务端识别到相同内容时会发生秒传（`rapid`），此时不消耗分片流量。
- 已完成的文件在重跑时直接跳过；本地文件在计划生成后发生变化（大小或内容）会被拒绝。
- 上传源与目标同样受安全约束：拒绝符号链接、绝对化远程路径、拒绝 `..`；凭证与签名地址不写入报告。

上传协议参考（`api/quota`、`api/precreate`、`pcs/superfile2`、`api/create`、分片与 slice-md5 语义）来自[固定版本的 BaiduPCS-Go](https://github.com/qjfoidnh/BaiduPCS-Go/tree/d827497caf359671eb4d21fac07ec3ac09d889e0)（Apache-2.0），作为公开数据在实现时核对，运行时不请求、不执行该源码。

## Python 调用与测试

公开函数接收普通字典及调用方持有的会话。可使用 `api.session`、`api.scan_share`、`model.groups`、`model.build_plan` 和 `model.validate_plan` 完成发现与计划生成。`cli.run_download` 仅用于执行**已经人工确认**的计划；调用方还必须像 `cli.download` 一样落实批准检查和实时源文件核对。希望由工具完整负责这些步骤时，请使用命令行入口。

```sh
uv run python -m unittest discover -s tests -v
```

测试使用临时目录和模拟的网盘响应，包含离线命令行全流程，不需要真实账号。本次打包验证不代表重新执行了真实网盘下载。

公开协议参考资料来自[固定版本的 OpenList 百度分享插件](https://github.com/OpenListTeam/openlist-baidushare-plugin/tree/8515b0f7bfa715007c833d1374fdb10b65fcc217)，其 MIT 许可原文保留在仓库根目录的 `THIRD-PARTY-LICENSE.txt`。已修复的协议差异包括账号同步使用 `no` 状态字段、分享短链接保留开头的 `1`，以及签名前对分享授权 JSON 进行表单编码。运行时不依赖 fundrive-baidu SDK 或本地 SDK 补丁。
