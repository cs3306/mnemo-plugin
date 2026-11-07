# mnemo-plugin

[中文](#%E4%B8%AD%E6%96%87) | [English](#english)

---

## 中文

[mnemo](https://github.com/cs3306/mnemo)（自托管 Telegram 机器人）的工具插件服务。mnemo 自己不做文档处理；它在启动时向本服务询问“有哪些工具”，把这些工具交给大模型，模型决定何时调用，生成的文件由 mnemo 直接发到 Telegram。

内置工具：

| 工具 | 作用 | 依赖 |
|---|---|---|
| `docx_create` | Markdown → Word（中文字体、公式变成原生 Word 公式、表格、目录） | pandoc |
| `pptx_create` | Markdown → PowerPoint（`##` 分页、章节页、演讲者备注、表格） | pandoc |
| `xlsx_create` | 多工作表 Excel：表头样式、冻结首行、筛选、公式、日期与数字格式、自动列宽 | 无 |
| `pdf_create` | Markdown → PDF（XeLaTeX，中文、公式、代码块、目录） | pandoc + XeLaTeX |
| `latex_create` | 编译完整 `.tex`（xelatex / lualatex / pdflatex），失败时把日志里的错误返回给模型重试，成功时 PDF 与源码一起发出 | TeX Live |
| `image_generate` | 调用 OpenAI 兼容的 `/images/generations`（本地 Stable Diffusion / FLUX 网关等） | 需要你自己有图像后端 |
| `schedule_add` / `schedule_list` / `schedule_remove` | 定时任务：对 bot 说“每天 7:30 给我发晨报，内容是……”，它自己建；说“取消晨报”就删。到点由本服务调用 mnemo 的 `/v1/trigger`，bot 带着记忆、搜索和其它工具完成任务并发到 Telegram | mnemo 开启 API（`MNEMO_URL`、`MNEMO_API_KEY`）+ 数据卷 |

DeepSeek 没有图像生成模型，所以 `image_generate` 只有在设置了 `IMAGE_BASE_URL` 时才出现。缺少依赖的工具不会出现在列表里，原因见 `GET /healthz`。

### 快速开始

和 mnemo 放进同一个 compose 文件（这样 mnemo 能用服务名访问）：

```yaml
services:
  mnemo-plugin:
    image: cs3306/mnemo-plugin:latest      # 不需要 PDF / LaTeX 时用 :latest-slim，体积小得多
    restart: unless-stopped
    environment:
      PLUGIN_TOKEN: ${PLUGIN_TOKEN}
    read_only: true
    tmpfs: ["/tmp:size=512m"]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
```

mnemo 的 `.env`：

```
PLUGIN_URLS=http://mnemo-plugin:8090
PLUGIN_TOKEN=<与上面相同，openssl rand -hex 24 生成>
```

重启后给 bot 发 `/plugins` 查看已发现的工具；之后直接说“把这些整理成 Word”“做个 Excel 记账表”“画一张……”。`/plugins reload` 可以在插件更新后重新发现。
也可以单独用本仓库的 `compose.yml` 运行，再把 `PLUGIN_URLS` 指向它的地址。

### 配置（环境变量）

见 [`.env.example`](.env.example)。常用的：`PLUGIN_TOKEN`（必填；不设置服务拒绝启动，除非 `PLUGIN_ALLOW_ANONYMOUS=true`）、`MAX_CONCURRENCY`（同时处理的调用数，默认 2）、`TOOL_TIMEOUT`、`MAX_FILE_MB`（默认 30，Telegram 机器人上限 50）、`DOCX_CJK_FONT`、`PDF_CJK_FONT`、`IMAGE_*`、`MNEMO_URL` / `MNEMO_API_KEY` / `DATA_DIR`（定时任务）。

### 定时任务

需要 mnemo ≥ 带 `POST /v1/trigger` 的版本。mnemo 侧：`.env` 里 `API_ADDR=:8088` 和 `API_KEY=…`（不要把该端口暴露到公网）。本服务侧：

```
MNEMO_URL=http://mnemo:8088
MNEMO_API_KEY=<与 mnemo 的 API_KEY 相同>
DATA_DIR=/data                    # 镜像里默认；compose 里挂一个卷，任务才能在重启后保留
SCHEDULE_TZ=                      # 可选；留空则用 mnemo 的 TZ（每次调用都会带过来）
SCHEDULE_GRACE_MIN=120            # 服务停机错过的任务，在这个窗口内补发，超过就跳过
```

任务存在 `$DATA_DIR/schedules.json`（原子写入）。支持“每天”、“每周几”、“某天一次”，精度到分钟，最多 20 个；mnemo 暂时连不上时指数退避重试，4xx（如密钥错误）不重试并记在 `last_error`（`schedule_list` 可见）。**到点跑的 prompt 是模型当时写下的**，其中可能混入网页里的注入内容，所以建任务后用 `schedule_list` 看一眼完整指令。

### 自己写工具

**Python**：写一个 `.py` 放进目录，挂载到 `EXTRA_TOOLS_DIR`（compose 里 `./extra_tools:/extra_tools:ro`），重启插件容器，在 Telegram 里 `/plugins reload`。不需要改本项目，也不需要重新构建镜像。

```python
from mnemo_plugin import tool, Result, File, ToolError

@tool("word_count", "统计文本字数。", {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
})
def word_count(args, ctx):          # ctx.chat / ctx.channel / ctx.config
    n = len(args["text"].split())
    return Result(text=f"{n} 个词")            # 要发文件：Result(text=..., files=[File(name, mime, bytes)])
```

参数按 JSON Schema 的常用子集校验（type、required、enum、items、minItems/maxItems、maxLength），出错时模型会看到明确的中文提示。抛出 `ToolError("原因")` 会把原因原样交给模型，让它修正后重试；其他异常只写日志，不泄露给模型。`examples/extra_tools/hello.py` 是一个完整例子（生成 `.ics` 日历事件）。

**任何语言**：只需实现下面三个 HTTP 接口，然后把它的地址加进 mnemo 的 `PLUGIN_URLS`（逗号分隔，可以有多个插件服务）。

### 协议（v1）

所有 `/v1` 请求带 `Authorization: Bearer <PLUGIN_TOKEN>`。

```
GET  /v1/tools
  → {"tools": [{"name": "word_count", "description": "...", "parameters": {<JSON Schema, type=object>}}]}

POST /v1/tools/{name}
  ← {"arguments": {...}, "context": {"chat": 123, "channel": "telegram"}}      # channel: "telegram" 或 "api"
  → 200 {"text": "给模型看的结果", "files": [{"name": "a.docx", "mime": "...", "data": "<base64>"}]}
  → 非 200 / 或带 "error"：{"error": "给模型看的原因"}
```

工具名 `^[a-zA-Z][a-zA-Z0-9_-]{0,63}$`，不能与 mnemo 内置工具（`web_search`、`web_fetch`）重名；多个服务提供同名工具时先发现的生效。`channel` 为 `api` 时文件不会被送达（OpenAI 兼容 API 没有文件通道），mnemo 会告诉模型“已生成但无法发送”。单次最多 8 个文件，单个文件 40 MB 以内。

### 安全

- **模型的输出不可信**（网页内容里的提示注入也可能影响它），所以：Markdown 里的图片被丢弃（否则 pandoc 会读取本地文件或请求任意 URL，可被用来外传数据或访问内网）；YAML 头、原始 TeX/HTML 被禁用；`latex_create` 禁止 shell escape，且 TeX 只能读写工作目录（`openin_any=p`）。测试里有对应用例。
- 令牌用常量时间比较；未授权请求不会触发任何工具。服务不对外暴露端口时（只在 compose 网络内），风险最小；需要暴露请放在反向代理/内网之后。
- 容器以非 root（uid 65532）、只读根文件系统运行；每次调用在独立的临时目录里完成并清理。
- `latex_create` 本质上是在执行模型写的 TeX 源码。以上限制挡住了常见的越权方式，但它不是强沙箱；不信任的场景请用 `:latest-slim`（没有 TeX）。

### 限制

- 文档里**不能插入图片**（刻意如此，见上）。`image_generate` 生成的图片单独发给用户。
- 还不能**读取**用户发来的文件（Word / PDF / Excel 的内容）——这需要 mnemo 先支持接收文件，后续再做。
- Excel 里的公式不在服务端计算，要在 Excel / WPS 打开时才有结果（Telegram 内置预览看不到）。
- PowerPoint 使用 pandoc 默认模板，中文字体由 PowerPoint 自动回退；版式朴素。
- 已验证环境：Ubuntu 24.04（pandoc 3.1、TeX Live 2023、Python 3.12）上的 49 项测试。**Dockerfile 与 GitHub Actions 的构建流程没有实际构建过**，首次构建请留意日志；`image_generate` 只对照假后端测过，没连过真实的图像服务。

### 开发

```bash
pip install -e '.[test]' && pytest        # 需要 pandoc、xelatex（缺少时相关测试自动跳过）
PLUGIN_TOKEN=dev python -m mnemo_plugin
```

---

## English

A tool plugin server for [mnemo](https://github.com/cs3306/mnemo), the self-hosted Telegram bot. mnemo does no document work itself: at startup it asks this service which tools exist, offers them to the LLM, and the files a tool produces are sent straight to Telegram.

Built-in tools:

| Tool | Does | Needs |
|---|---|---|
| `docx_create` | Markdown → Word (CJK font, native Word equations, tables, TOC) | pandoc |
| `pptx_create` | Markdown → PowerPoint (`##` per slide, section slides, speaker notes, tables) | pandoc |
| `xlsx_create` | Multi-sheet Excel: styled header, frozen row, filter, formulas, date/number formats, auto width | nothing |
| `pdf_create` | Markdown → PDF via XeLaTeX (Chinese, math, code blocks, TOC) | pandoc + XeLaTeX |
| `latex_create` | Compile a full `.tex` (xelatex / lualatex / pdflatex); on failure the log's errors go back to the model to retry; PDF and source are sent together | TeX Live |
| `image_generate` | Any OpenAI-compatible `/images/generations` backend (local Stable Diffusion / FLUX gateway, …) | an image backend of your own |

DeepSeek has no image model, so `image_generate` appears only when `IMAGE_BASE_URL` is set. A tool whose dependency is missing is not offered; `GET /healthz` says why.

### Quick start

Add it to mnemo's compose file so mnemo can reach it by service name:

```yaml
services:
  mnemo-plugin:
    image: cs3306/mnemo-plugin:latest      # :latest-slim is much smaller but has no PDF / LaTeX
    restart: unless-stopped
    environment:
      PLUGIN_TOKEN: ${PLUGIN_TOKEN}
    read_only: true
    tmpfs: ["/tmp:size=512m"]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
```

In mnemo's `.env`:

```
PLUGIN_URLS=http://mnemo-plugin:8090
PLUGIN_TOKEN=<same value; openssl rand -hex 24>
```

Restart, then send `/plugins` to the bot to see the discovered tools, and ask for what you want ("turn these notes into a Word file", "make an Excel budget"). `/plugins reload` re-discovers after an update. This repo's `compose.yml` also runs the plugin stand-alone; point `PLUGIN_URLS` at its address.

### Configuration

See [`.env.example`](.env.example). Main ones: `PLUGIN_TOKEN` (required; the server refuses to start without it unless `PLUGIN_ALLOW_ANONYMOUS=true`), `MAX_CONCURRENCY` (default 2), `TOOL_TIMEOUT`, `MAX_FILE_MB` (default 30; Telegram bots allow 50), `DOCX_CJK_FONT`, `PDF_CJK_FONT`, `IMAGE_*`.

### Scheduled tasks

Needs a mnemo build with `POST /v1/trigger`. On mnemo set `API_ADDR=:8088` and `API_KEY=…` (never expose that port to the internet). Here:

```
MNEMO_URL=http://mnemo:8088
MNEMO_API_KEY=<same as mnemo's API_KEY>
DATA_DIR=/data                    # default in the image; mount a volume or tasks vanish on recreate
SCHEDULE_TZ=                      # optional; empty = mnemo's TZ (sent with every call)
SCHEDULE_GRACE_MIN=120            # a task missed while this server was down is still run within this window, else skipped
```

Tasks live in `$DATA_DIR/schedules.json` (atomic writes). Daily, weekly (chosen weekdays) or one-off, minute precision, at most 20. If mnemo is unreachable the call is retried with exponential backoff; 4xx (e.g. wrong key) is not retried and shows up as `last_error` in `schedule_list`. **The prompt that runs is whatever the model wrote at creation time** and may contain text injected from a web page, so check the full instruction with `schedule_list` after creating a task.

### Writing your own tools

**Python**: drop a `.py` file into a directory mounted at `EXTRA_TOOLS_DIR` (compose: `./extra_tools:/extra_tools:ro`), restart the plugin container, send `/plugins reload`. No change to this project and no image rebuild.

```python
from mnemo_plugin import tool, Result, File, ToolError

@tool("word_count", "Count the words in a text.", {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
})
def word_count(args, ctx):          # ctx.chat / ctx.channel / ctx.config
    return Result(text=f"{len(args['text'].split())} words")   # files: Result(text=..., files=[File(name, mime, bytes)])
```

Arguments are validated against a common JSON-Schema subset (type, required, enum, items, minItems/maxItems, maxLength) with clear messages for the model. Raising `ToolError("reason")` hands the reason to the model so it can fix the call; any other exception is logged and never shown to the model. `examples/extra_tools/hello.py` is a complete example (`.ics` calendar events).

**Any language**: implement the three HTTP endpoints below and add the URL to mnemo's `PLUGIN_URLS` (comma-separated; several plugin servers are fine).

### Protocol (v1)

Every `/v1` request carries `Authorization: Bearer <PLUGIN_TOKEN>`.

```
GET  /v1/tools
  → {"tools": [{"name": "word_count", "description": "...", "parameters": {<JSON Schema, type=object>}}]}

POST /v1/tools/{name}
  ← {"arguments": {...}, "context": {"chat": 123, "channel": "telegram"}}      # channel: "telegram" | "api"
  → 200 {"text": "result for the model", "files": [{"name": "a.docx", "mime": "...", "data": "<base64>"}]}
  → non-200, or a body with "error": {"error": "reason for the model"}
```

Tool names match `^[a-zA-Z][a-zA-Z0-9_-]{0,63}$` and must not collide with mnemo's built-ins (`web_search`, `web_fetch`); if two servers offer the same name, the first one discovered wins. On the `api` channel files cannot be delivered (the OpenAI-compatible API has no file channel), and mnemo tells the model so. At most 8 files per call, 40 MB each.

### Security

- **Model output is untrusted** (prompt injection from a web page can steer it). So: Markdown images are dropped (otherwise pandoc would read local files or request arbitrary URLs: a way to exfiltrate data or reach internal services); YAML headers and raw TeX/HTML are disabled; `latex_create` runs without shell escape and TeX may only read and write the working directory (`openin_any=p`). Tests cover each.
- The token is compared in constant time; an unauthorised request triggers nothing. Keep the port inside the compose network, or behind a reverse proxy / LAN, rather than on the internet.
- The container runs as non-root (uid 65532) on a read-only filesystem; every call works in its own temp directory, removed afterwards.
- `latex_create` executes TeX written by the model. The limits above block the common escapes but this is not a hard sandbox; where that matters, use `:latest-slim` (no TeX).

### Limitations

- **No images inside documents** (deliberate, see above). Images from `image_generate` are sent on their own.
- It cannot yet **read** files the user sends (Word / PDF / Excel); that needs mnemo to accept incoming files first.
- Excel formulas are not evaluated on the server; they calculate when opened in Excel / WPS (Telegram's preview shows blanks).
- PowerPoint uses pandoc's default template: plain layout, CJK font left to PowerPoint's fallback.
- Verified: 49 tests on Ubuntu 24.04 (pandoc 3.1, TeX Live 2023, Python 3.12). **The Dockerfile and the GitHub Actions build have not actually been run**; watch the first build's log. `image_generate` was tested against a fake backend only, not a real image service.

### Development

```bash
pip install -e '.[test]' && pytest        # needs pandoc and xelatex (those tests skip when missing)
PLUGIN_TOKEN=dev python -m mnemo_plugin
```
