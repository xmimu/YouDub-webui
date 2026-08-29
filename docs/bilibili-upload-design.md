# Bilibili 投稿接入设计

## 背景与目标

YouDub 已能生成译制后的视频和发布简介。本设计通过 [biliup](https://github.com/biliup/biliup) 将最终视频投稿到 Bilibili，并满足以下目标：

- 支持 WebUI 扫码登录，不向浏览器暴露 Cookie 或 token。
- 支持任务完成后自动投稿，也支持详情页手动投稿。
- 投稿标题、简介、标签自动生成，分区使用管理员配置的固定 `tid`。
- URL 任务统一按转载投稿，并附原视频 URL。
- 投稿副作用与可重跑的视频处理流水线隔离，避免阶段重做导致重复投稿。
- 对超时、进程退出和应用重启采取保守策略，优先避免重复投稿。

本地视频没有可验证的公开来源 URL，因此不允许投稿。

## 核心决策

### 独立上传任务

Bilibili 投稿不属于 pipeline stage。pipeline 支持恢复、重跑和单阶段重做，如果把投稿加入 pipeline，可能在恢复时重复发布。

投稿由独立的 `bilibili_upload_jobs` 状态机和单线程 worker 管理。pipeline 成功后只负责按任务开关创建上传任务。

### biliup 集成边界

- 固定运行依赖 `biliup==1.2.4`。
- 二维码登录使用 `stream_gears.get_qrcode(None)` 和 `stream_gears.login_by_qrcode(payload, None)`。
- 视频投稿使用独立子进程调用 `python -m biliup`，避免原生扩展或长时间上传阻塞 FastAPI 主进程。
- 子进程通过参数数组启动，不使用 shell，也不接受前端传入的文件路径或任意 CLI 参数。
- 投稿接口固定为 biliup 的 `web` submit 模式。

### 防止重复投稿

同一任务存在以下状态的上传记录时，不允许创建新投稿：

- `queued`
- `running`
- `succeeded`
- `unknown`

只有明确发生在上传前的 `failed` 状态允许重试。

已经开始调用 biliup 后发生异常、超时或应用重启时，远端是否接受稿件无法可靠判断，因此记录为 `unknown`。用户必须先到 Bilibili 创作中心确认，系统不会自动重试。

## 系统结构

```text
任务创建
  -> tasks.auto_upload_bilibili
  -> 视频处理 pipeline
  -> summary 生成发布元数据
  -> pipeline succeeded
       -> 自动开关关闭：等待手动投稿
       -> 自动开关开启：创建 bilibili_upload_job
  -> Bilibili 单线程 worker
  -> biliup 子进程
  -> Bilibili
```

主要模块：

| 模块 | 职责 |
| --- | --- |
| `backend/app/adapters/biliup.py` | 封装二维码接口、凭据校验和 biliup 投稿命令。 |
| `backend/app/bilibili_auth.py` | 管理内存二维码登录会话和私有凭据写入。 |
| `backend/app/bilibili_uploads.py` | 投稿校验、任务创建和单线程上传 worker。 |
| `backend/app/adapters/summary.py` | 生成 Markdown 和稳定的 Bilibili 发布元数据。 |
| `backend/app/database.py` | 保存任务自动投稿开关、设置和上传任务状态。 |
| `backend/app/main.py` | 暴露设置、登录和投稿 API。 |

## 数据设计

### tasks 扩展

`tasks.auto_upload_bilibili INTEGER NOT NULL DEFAULT 0`

该字段在创建 URL 任务时确定。任务重跑会保留此配置，但既有成功或结果未知的投稿记录仍会阻止再次投稿。

### channel_jobs 扩展

`channel_jobs.auto_upload_bilibili INTEGER NOT NULL DEFAULT 0`

频道批量任务创建视频任务时继承该值。

### bilibili_upload_jobs

| 字段 | 说明 |
| --- | --- |
| `id` | 上传任务 UUID。 |
| `task_id` | 对应 YouDub 任务。 |
| `status` | `queued`、`running`、`succeeded`、`failed` 或 `unknown`。 |
| `title` | 创建上传任务时保存的标题快照。 |
| `description` | 简介快照。 |
| `tags_json` | 标签快照。 |
| `tid` | 投稿分区快照。 |
| `source_url` | 转载来源 URL。 |
| `result_message` | 成功结果摘要。 |
| `error_message` | 错误或未知状态说明。 |
| `created_at`、`started_at`、`completed_at` | 生命周期时间。 |

元数据采用快照，避免排队期间设置或 summary 文件变化导致实际投稿内容漂移。

## 状态机

```text
queued
  -> failed       上传前校验失败，可以重试
  -> running      开始执行 biliup

running
  -> succeeded    biliup 明确成功退出
  -> unknown      超时、异常或应用重启，禁止直接重试
```

应用启动时：

- 遗留 `queued` 标记为 `failed`，因为尚未开始上传。
- 遗留 `running` 标记为 `unknown`，因为远端结果不确定。

## 登录与凭据安全

凭据路径为 `data/cookies/bilibili.json`。

扫码流程：

1. 前端请求创建二维码登录会话。
2. 后端通过 biliup 获取二维码 URL，返回会话 ID 和二维码 URL。
3. 后端 daemon thread 等待扫码确认。
4. biliup 返回登录凭据后，后端再次检查该会话仍为 `pending`。
5. 使用原子写入保存凭据，并保持 owner-only 文件权限。
6. 前端轮询会话状态，只会收到状态和错误，不会收到凭据。

开始新登录或断开账号时，已有 pending 会话会被取消。已取消会话即使随后完成，也不能覆盖当前凭据。

## 发布元数据

summary 阶段除原有 Markdown 外，还会生成：

```text
<session>/metadata/bilibili_publish.json
```

结构：

```json
{
  "title": "【AI译制】标题",
  "description": "自动生成的简介，含原作者、原视频发布时间与原视频链接",
  "tags": ["科技", "AI译制", "中文配音"],
  "source_url": "https://www.youtube.com/watch?v=...",
  "original_title": "Original title",
  "uploader": "Creator"
}
```

约束：

- 标题最多 80 个字符。
- 简介最多 2000 个字符，包含 AI 译制说明、内容简介，以及原作者、原视频发布时间（`upload_date`，无则省略）和原视频链接。
- 标签去重后最多 10 个。
- 投稿时 `copyright=2`。
- 实际 `source_url` 使用任务已校验的规范 URL，而不是前端输入或文件内容。
- 分区来自设置中的 `bilibili.default_tid`。

### 封面

summary 阶段会尝试自动生成投稿封面：

1. 用 ffmpeg 从 `media/video_source.mp4` 截取中间一帧。
2. 用 Pillow 将画面缩放并中心裁剪为 3:4 竖版（1080×1440）。
3. 底部叠加半透明黑色渐变，叠加上中文标题（去掉「【AI译制】」前缀），自动换行并自适应字号。
4. 输出 `metadata/cover.jpg`，并将 `cover_path` 写入 `bilibili_publish.json`。

- 中文字体按平台搜索：macOS `STHeiti`/`PingFang`，Windows `msyh`/`simhei`，Linux `Noto Sans CJK`/文泉驿；找不到字体或无视频帧时跳过文字/封面。
- 封面文件路径会作为快照保存到投稿任务，投稿时通过 biliup `--cover` 上传。
- 任何一步失败都回退为无封面，不影响投稿。


## API

### 设置和登录

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/settings/bilibili` | 获取连接状态和默认 `tid`。 |
| `POST` | `/api/settings/bilibili` | 保存默认 `tid`。 |
| `POST` | `/api/settings/bilibili/login/qrcode` | 创建二维码登录会话。 |
| `GET` | `/api/settings/bilibili/login/qrcode/{session_id}` | 查询扫码状态。 |
| `DELETE` | `/api/settings/bilibili/login` | 取消 pending 登录并删除凭据。 |

### 投稿

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/tasks/{task_id}/uploads/bilibili` | 获取该任务最新投稿状态。 |
| `POST` | `/api/tasks/{task_id}/uploads/bilibili` | 创建手动投稿任务。 |

所有接口沿用 YouDub 的会话认证和 CSRF 防护。

## 投稿前校验

创建上传任务必须同时满足：

- 原任务存在且状态为 `succeeded`。
- 不是 `local://` 本地视频任务。
- `final_video_path` 是 `WORKFOLDER` 内存在的普通文件。
- session 是 `WORKFOLDER` 内存在的目录。
- Bilibili 凭据文件存在且结构有效。
- 默认分区是 `1..65535` 的整数。
- 发布元数据存在且标题非空。
- 没有不可重复投稿的既有记录。

worker 在实际上传前会再次检查最终视频和凭据，降低排队期间文件或配置变化带来的风险。

## 前端行为

- 设置页展示账号连接状态、二维码、断开连接和默认分区输入框。
- 首页 URL 任务提供“完成后自动投稿”复选框，默认关闭。
- 本地视频选择后自动投稿选项被禁用。
- 频道批量处理和频道单视频创建任务支持相同选项。
- 成功任务详情页提供手动投稿按钮并轮询上传状态。
- 本地任务不显示投稿按钮。
- `failed` 状态允许重试；`succeeded` 和 `unknown` 不显示重试按钮。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BILIBILI_DEFAULT_TID` | 空 | 默认投稿分区；设置页保存值优先。 |
| `BILIUP_UPLOAD_TIMEOUT_SECONDS` | `86400` | 单次 biliup 子进程最大运行秒数，最低 60 秒。 |

## 测试与验证

专项后端测试位于 `backend/tests/test_bilibili.py`，覆盖：

- biliup 二维码 Python 绑定参数契约。
- 凭据不会通过登录状态响应泄露。
- 发布元数据文件生成及约束。
- 上传任务去重与明确失败后的重试。
- worker 成功状态写入。
- 重启后 `running -> unknown`，并阻止再次创建投稿。

前端测试覆盖任务级自动投稿选项、设置保存和原有轮询行为。常规验证命令：

```bash
.venv/bin/pytest backend/tests
npm --prefix apps/web test
npm --prefix apps/web run lint
npm --prefix apps/web run build
```

## 已知限制

- biliup 1.2.4 的 CLI 没有提供稳定的机器可读 BV 号输出，因此首版只记录投稿提交成功，不展示稿件链接。
- `unknown` 状态暂不支持自动查询 Bilibili 创作中心进行对账，需要人工确认。
- 默认分区通过设置页的动态分区下拉选择（数据来自 Bilibili 接口，缓存 1 小时）；拉取失败时可手填数字 `tid`。
- 封面字体依赖本机安装的中文字体；找不到字体时会跳过封面文字或整个封面。
