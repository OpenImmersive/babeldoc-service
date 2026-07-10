# babeldoc-service — BabelDOC / pdf2zh-next 翻译微服务

OpenImmersive / yulingling 的 PDF 全文翻译引擎层。一个刻意做薄的 HTTP wrapper，
以子进程方式调用 [pdf2zh-next](https://github.com/PDFMathTranslate/PDFMathTranslate-next)
的 `pdf2zh` CLI（翻译引擎为 [BabelDOC](https://github.com/funstory-ai/BabelDOC)），
产出保留排版的翻译版 PDF（单语 mono / 双语对照 dual）。

## 为什么这一层整层开源（AGPL 合规）

pdf2zh-next 与 BabelDOC 均为 **AGPL-3.0** 许可。AGPL section 13 要求：通过网络向
用户提供基于 AGPL 代码的服务时，必须向用户提供对应源码。我们的隔离策略：

1. **本目录（wrapper 层）整层开源**——`wrapper.py` / `Dockerfile` / `compose.yml` /
   `client.mjs` 全部公开，加上上游源码链接，即满足 section 13 的源码提供义务。
2. **wrapper 层零业务逻辑**——不做计费、鉴权、配额。这些全部留在闭源的 Node 网关里，
   网关只通过 HTTP 与本服务通信（arm's-length）。
3. **与 AGPL 代码的边界是 CLI 子进程**——wrapper 只 spawn `pdf2zh` 命令，绝不
   `import pdf2zh_next`，保持"聚合（mere aggregation）"而非"衍生作品"的最干净边界。

维护红线：**不要**在本目录加任何业务逻辑或专有信息；**不要**改成 import 方式调用。

上游源码：
- pdf2zh-next: https://github.com/PDFMathTranslate/PDFMathTranslate-next
- BabelDOC: https://github.com/funstory-ai/BabelDOC

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/translate` | multipart 上传：`file`（PDF 必填）、`lang_out`（默认 `zh`）、`pages`（如 `1-3` / `1,3,5-`）、`qps`（默认 2，上限 10）。返回 `{"id": "..."}` |
| GET | `/status/<id>` | `queued` / `processing` / `done` / `failed` + 日志尾部 |
| GET | `/result/<id>` | 返回翻译 PDF，默认单语 mono；`?variant=dual` 取双语对照版 |
| GET | `/health` | 存活探针 + 队列长度 |

任务串行执行（单并发，CPU 推理机器上并发只会互相拖慢）。任务与状态落盘在
`$DATA_DIR/jobs/<id>/`，重启后 queued/processing 任务自动重新入队；24 小时后自动清理。

## 本地跑

```bash
python3 -m venv venv && venv/bin/pip install pdf2zh-next   # torch CPU 很大，耐心等
venv/bin/python wrapper.py                                  # 默认 0.0.0.0:21012
# 若 CLI 不在 PATH：PDF2ZH_BIN=venv/bin/pdf2zh venv/bin/python wrapper.py
```

冒烟：

```bash
curl -F file=@test.pdf -F pages=1-2 http://127.0.0.1:21012/translate   # -> {"id":"..."}
curl http://127.0.0.1:21012/status/<id>
curl -o out.pdf http://127.0.0.1:21012/result/<id>
```

首次运行会自动下载 DocLayout-YOLO 布局模型和字体（数百 MB，缓存在 `~/.cache/babeldoc`）。

## 部署到服务器

```bash
docker compose up -d --build
```

- 容器名 `yll_babeldoc`，host 端口 **21012**。
- 名卷 `yll_babeldoc_cache` 挂在 `/root/.cache`——模型/字体缓存，不挂的话每次
  rebuild 都会重新下载几百 MB。
- 名卷 `yll_babeldoc_data` 挂在 `/data`——任务与产物，rebuild 不丢。
- 本服务无鉴权，**只应由 Node 网关访问**；生产上不要把 21012 暴露到公网
  （防火墙只放行内网，或把 ports 改成 `127.0.0.1:21012:21012`）。

Node 侧接入示例见 `client.mjs`（上传 → 轮询 → 下载，供 server.mjs 按文档类型路由用）。

**首次资产下载的坑（实测踩过）**：pdf2zh 首次运行的 warmup 会下载 DocLayout-YOLO
模型（~75MB）+ 26 个 CJK 字体（~300MB），其内置 httpx 超时偏短，慢网络下会反复
`ReadTimeout/ConnectTimeout` 直接失败。若容器日志出现 `asset coroutine failed:
RetryError`，手动预热缓存即可（下到 `/root/.cache/babeldoc/`，即名卷内）：

```bash
# 模型
docker exec yll_babeldoc sh -c 'mkdir -p /root/.cache/babeldoc/models && \
  curl -L -o /root/.cache/babeldoc/models/doclayout_yolo_docstructbench_imgsz1024.onnx \
  "https://huggingface.co/wybxc/DocLayout-YOLO-DocStructBench-onnx/resolve/main/doclayout_yolo_docstructbench_imgsz1024.onnx?download=true"'
# 字体从 https://huggingface.co/datasets/awwaawwa/BabelDOC-Assets fonts/ 目录同理，
# 下到 /root/.cache/babeldoc/fonts/（文件名照 babeldoc 报错/元数据里的名字）。
```

缓存就位后任务即正常；名卷保证 rebuild 后不用重下。

## 翻译引擎配置

默认引擎 **Google**（免费、无需 API key），通过环境变量 `TRANSLATE_ENGINE` 切换，
取值即 pdf2zh CLI 的引擎 flag 名（`google` / `bing` / `deepl` / `openai` ...）。

引擎的附加参数（如 DeepL 的 API key）按 pdf2zh-next 的约定用环境变量传入容器：
CLI 选项 `--xxx-yyy` 对应环境变量 `PDF2ZH_XXX_YYY`。例如 DeepL：

```yaml
environment:
  TRANSLATE_ENGINE: deepl
  PDF2ZH_DEEPL_AUTH_KEY: "..."   # 具体变量名以 pdf2zh --help 输出为准
```

`qps` 请求参数控制对翻译服务的每秒请求数，免费 Google 引擎建议保持低值（默认 2）。

## 已知限制

- **不做图片内文字 OCR**——扫描件/图内文字不翻译（上游限制）。
- 表格翻译在上游是 experimental，复杂表格排版可能劣化。
- 单并发：大文件排队时间 = 前面任务耗时之和，网关侧应向用户展示排队状态。
- 首次请求前需等模型下载完成（看容器日志）。
