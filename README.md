# zhihu-archiver

一个用于将知乎用户公开可访问的**文章（Articles）**与**想法（Pins）**归档到本地 SQLite、JSONL 和 Markdown 的 Python 命令行工具，支持断点续传、去重、截断正文自动补全、转发/引用原文与作者附言分离，以及可验证的样本模式。

## 特性

- **样本先行（`--mode sample`）**：先拉取文章和想法各 2 页，并各抽取 1 条详情进行交叉核对，输出截断比例与全量请求/费用预估。
- **断点续传与事务安全**：使用 SQLite（`archive.db`）在同一事务中写入单页数据与翻页断点；中断重跑自动续接并重试失败条目，按 `(type, id)` 严格去重。
- **高保真 Markdown 导出**：保留段落、标题、列表、链接（自动还原 `link.zhihu.com` 跳转）、引文、代码块、LaTeX 公式与图片链接；对含转发/引用的想法将**作者附言**与**引用原文**分开呈现。
- **双数据源 Provider 架构**：
  - **`--provider browser`（默认，完全免费、免 JS 逆向）**：通过 Playwright / Chrome CDP（`--cdp-url auto`）复用本地已登录的 Chrome，以低速人工间隔自动滚动并拦截知乎官方 `/api/v4/` JSON 响应。
  - **`--provider tikhub`（可选托管 API）**：读取环境变量 `TIKHUB_API_KEY`，适合无浏览器环境。
- **隐私友好**：`.gitignore` 默认忽略 `output/`、`data/`、`.browser_profile/`、`*.db`、`*.jsonl` 和 `reading-notes.md`，方便开源代码而不同步个人归档语料。

## 环境要求

- Python 3.11+（推荐通过 [`uv`](https://docs.astral.sh/uv/) 运行）
- 默认 `browser` 模式：本地安装 Chrome/Edge；推荐使用 Chrome 地址栏 `chrome://inspect/#remote-debugging` 开启本地远程调试并搭配 `--cdp-url auto`
- 可选 `tikhub` 模式：环境变量 `TIKHUB_API_KEY`（不会写入代码或日志）

## 快速开始（Windows PowerShell）

### 1. 安装依赖

```powershell
uv sync --extra dev
```

### 2. 开启本地 Chrome 远程调试（免费模式推荐）

在已登录知乎的 Chrome 地址栏打开 `chrome://inspect/#remote-debugging`，勾选 **Allow remote debugging for this browser instance**。

### 3. 运行样本模式（推荐先跑这一步）

```powershell
uv run zhihu-archiver https://www.zhihu.com/people/<user_url_token> --out output\<user_url_token> --mode sample --provider browser --cdp-url auto --fetch-user-info
```

检查终端输出的 `Sample Report`（翻页新 ID 数量、列表正文是否截断、预计全量请求数）及生成的 Markdown 样本后再执行全量归档。

### 4. 运行全量归档（支持分批慢速断点续传）

```powershell
# 归档全部文章、想法、提问，并按批次慢速归档回答（每批最多 25 次请求，每页随机间隔 3–5 秒）
uv run zhihu-archiver https://www.zhihu.com/people/<user_url_token> `
  --out output\<user_url_token> `
  --mode full `
  --types articles,pins,questions,answers `
  --provider browser `
  --cdp-url auto `
  --max-requests 25 `
  --min-delay 3.0 `
  --max-delay 5.0 `
  --download-images
```

若中途达到 `--max-requests` 上限、网络波动或遇到限流，直接重新执行同一条命令即可从断点继续，不会产生重复数据。

- 可用 `--types` 自由挑选内容类型（`articles,pins,answers,questions`）。
- 可用 `--answer-sort created|voteups` 控制回答按时间或按赞同数排序抓取。
- 导出结果除 `corpus.jsonl`、`manifest.json` 和单篇 Markdown 外，还会自动生成按时间排序的 `INDEX.md`（并标注有后续编辑/补充的条目）。

> 若改用 TikHub 托管 API，设置 `$env:TIKHUB_API_KEY = "..."` 后将 `--provider browser --cdp-url auto` 替换为 `--provider tikhub` 即可。

## 合规与使用说明

本工具仅用于个人学习、阅读与公开内容本地备份，请遵守平台服务条款与合理请求频率，切勿用于大规模商业抓取或二次分发他人版权内容。

### 5. 仅重新导出（不消耗 API 请求）

```powershell
uv run zhihu-archiver https://www.zhihu.com/people/<user_url_token> --out output\<user_url_token> --export-only
```

## 输出目录结构

```text
output/<user_url_token>/
├── archive.db        # SQLite 数据库（原始响应、断点、失败记录）
├── corpus.jsonl      # 统一语料文件（每行一条文章或想法 JSON）
├── manifest.json     # 归档统计（接口总数、去重数、完整正文数、时间范围、失败条目与来源链接）
├── articles/         # 单篇文章 Markdown（含 YAML frontmatter）
└── pins/             # 单条想法 Markdown（含作者附言与引用原文分区）
```

## 运行测试（无需真实 API Key）

```powershell
uv run pytest -v
```
