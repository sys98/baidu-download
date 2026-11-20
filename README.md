# 百度网盘下载工具

**先查看分享内容，再选择文件，确认后按清单下载。**

通过百度网盘分享链接下载指定视频和封面的 Python 命令行工具。支持单链接和批量任务，先列出候选文件供人工确认，再执行下载；中断后可续传，并记录下载速度、校验媒体文件。

<p>
  <img src="./assets/readme/workflow.svg" width="100%" alt="下载流程：scan 扫描并生成候选清单，plan 绑定选中的文件，download 经人工确认后下载到对应剧名目录。">
</p>

**本地验证情况：**27 项离线测试通过，覆盖完整命令行流程、真实本地 MP4 文件探测，以及上传的计划构建、额度拦截、分片与合并链路；已在 Python 3.12、3.14 环境验证，并完成独立安装包测试。这些检查不代表网盘接口始终可用，也不承诺下载或上传速度。

## 安装与环境检查

需要 **Python 3.11+**、**macOS 或 Linux**，以及**支持硬链接的下载目录**，例如 APFS、ext4。暂不支持 ExFAT/FAT。视频校验需要 **ffprobe**；读取固定版本的公开客户端协议参考资料需要 **GitHub CLI**，也可通过标准输入提供该资料。

```sh
git clone https://github.com/sys98/baidu-download.git
cd baidu-download
uv tool install .
baidu-download doctor
```

`doctor` 只检查本机环境，不登录账号、不发送网络请求。请按需安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)、[ffmpeg/ffprobe](https://ffmpeg.org/download.html) 和 [GitHub CLI](https://cli.github.com/)。仅下载封面时不需要 ffprobe，但 `doctor` 仍会报告它是否缺失。

## 单链接下载：扫描、选择、确认

### 1. 扫描分享内容

执行前两行时，分别输入分享链接和希望使用的剧名，然后运行扫描命令：

```sh
read -r SHARE_URL
read -r TITLE
baidu-download scan --url "$SHARE_URL" --title "$TITLE" --job ./job
```

打开 `job/review.md` 查看候选清单。扫描会遍历嵌套目录和所有分页，不下载媒体文件，也不转存到自己的网盘。账号凭证和提取码未通过进程环境提供时，使用隐藏输入提示获取。

### 2. 选择视频和封面

编辑自动生成的 `job/selection.json`，初始选择为空。确认第 1 组确实是目标视频目录后，可以按下面的方式选择：

```json
[
  {"source_id": "1", "groups": [1], "file_ids": [], "cover_ids": []}
]
```

只下载部分视频时，使用 `file_ids` 指定文件，不必选择整个组。需要封面时，把候选清单中的图片 ID 填入 `cover_ids`，可同时保留多个封面版本。

**文件放在同一目录，只能作为候选线索，不能证明内容相关。** 工具不会根据文件名或集数编号自动认定目标视频；同目录中的无关视频仍需人工排除。

### 3. 确认清单并下载

```sh
baidu-download plan --job ./job --destination ./media
# 执行下一条命令前，先查看 job/plan-review.md。
baidu-download download --job ./job --workers 4
```

输入工具显示的 `DOWNLOAD <plan_id>` 确认短语，批准本次具体范围。传输前会重新核对网盘文件的身份、大小和类型。文件保留原名，保存到 `media/<剧名>/`。

已经批准且范围未变的任务，可用同一条下载命令续跑。选择范围、目标目录或源文件变化后，需要重新审阅。只复用有匹配校验记录的完整文件，不覆盖无关文件或来源不明的临时文件。

## 查看进度、速度与校验结果

```sh
baidu-download status --job ./job
baidu-download verify --job ./job --deep
```

- 下载进度显示本轮新接收字节数和分段速度，单位为 MB/s。
- `speed.csv` 保留测速记录；复用的文件、续传前已有的字节不会计入本轮传输量。
- `verify --deep` 会探测视频流，并对图片完整解码、校验哈希；不包含视频逐帧完整解码。
- 最多使用 4 个独立会话并发下载；网盘服务返回异常时停止派发新任务，已确认归属的临时文件可以续传。

## 批量任务与脚本调用

批量输入是一个 JSON 数组，每条包含 `title`、`share_url`，以及可选的 `id`、`expected_episodes`、`extraction_code`。声明集数与选中文件数不一致时，会在批准前明确列出。

```sh
baidu-download scan --sources ./sources.json --job ./batch-job
```

后续审阅和选择步骤与单链接一致。批量格式、凭证输入、标准输入协议资料、Python 函数和故障恢复，请参阅[完整使用指南](./docs/usage.md)。运行时不需要安装 fundrive-baidu SDK，也不依赖对虚拟环境中的 SDK 文件打补丁。

## 上传到指定目录

把已下载或本地已有的文件传回网盘的指定目录，上传前自动校验额度：

```sh
baidu-download upload-plan --paths ./media/剧名 --remote-dir /备份/剧名 --job ./upjob
# 查看 upjob/plan-review.md：文件清单、字节数、额度余量。
baidu-download upload --job ./upjob
```

输入 `UPLOAD <plan_id>` 确认后，工具会再次核对额度，充足才启动上传；不足时直接拒绝并列出剩余额度。文件按 4 MB 分片并行上传并逐片校验，远程目录逐级创建，不覆盖同名文件。已上传的文件重跑时跳过，中断后可续跑。详见[使用指南](./docs/usage.md)。

## 登录凭证与使用限制

账号凭证通过隐藏输入或已有的进程环境变量 `BAIDU_BDUSS`、`BAIDU_STOKEN` 提供；可选变量 `BAIDU_SHARE_CODE` 用于提供提取码。工具不自动读取 `.env` 或浏览器凭证，生成的报告不保存账号凭证和带签名的下载地址。

网盘客户端接口可能变化，也可能要求账号验证。工具会报告这些失败，不自动识别验证码。请仅下载你有权访问和使用的文件。

## 开发与第三方声明

```sh
uv run python -m unittest discover -s tests -v
uv build
```

测试使用临时目录和模拟的网盘响应。真实本地 MP4 测试还需要 ffmpeg/ffprobe；缺少这些工具时，会明确跳过该项测试。

公开协议参考资料来自[固定版本的 OpenList 百度分享插件](https://github.com/OpenListTeam/openlist-baidushare-plugin/tree/8515b0f7bfa715007c833d1374fdb10b65fcc217)，其 MIT 许可声明保留在 [THIRD-PARTY-LICENSE.txt](./THIRD-PARTY-LICENSE.txt)。远程源码仅在内存中作为数据解析，不执行、不落盘。
