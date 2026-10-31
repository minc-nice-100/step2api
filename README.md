# step2api

[![ci](https://github.com/minc-nice-100/step2api/actions/workflows/ci.yml/badge.svg)](https://github.com/minc-nice-100/step2api/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.9%E2%80%933.14-blue)](https://github.com/minc-nice-100/step2api/actions/workflows/ci.yml)
[![dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen)](#)
[![license](https://img.shields.io/badge/license-Unlicense-lightgrey)](LICENSE)

把 **StepFun AI Studio（studio.stepfun.com）** 的网页会话封装成标准 **OpenAI / Anthropic 兼容 API**。
纯 Python 标准库实现，零第三方依赖；单进程 + 内置 Web 面板；多账号轮询、Token 自动续期。

> **English:** a zero-dependency, single-process gateway that exposes StepFun AI Studio as
> OpenAI-, Anthropic- and Responses-compatible endpoints (`/v1/chat/completions`, `/v1/messages`,
> `/v1/responses`, audio TTS/ASR), with an account pool, automatic token refresh, conversation
> affinity, and a built-in web panel. Docs below are in Chinese.

```
OpenAI SDK / Claude Code / Codex  ──►  step2api  ──►  studio.stepfun.com
                                      /v1/chat/completions
                                      /v1/messages
                                      /v1/responses
```

---

## 特性

- **两种上游模式**
  - `playground`（默认）：走 Studio 的普通对话通道，延迟低、支持 `system_prompt`、思考档位、联网搜索；
  - `agent`：走工作区 Agent 通道，模型可调用服务端工具（读写文件、执行命令等），适合让模型「动手做事」。
- **多协议入口**：`/v1/chat/completions`（含 SSE 流式）、`/v1/messages`（Anthropic，可直接接 Claude Code）、
  `/v1/responses`（Responses API）。
- **工具调用**：把 OpenAI / Anthropic 的 `tools` 自动注入系统提示，解析模型输出为结构化 `tool_calls` /
  `tool_use`，支持完整的「调用 → 客户端执行 → 回传结果 → 继续」闭环。
- **用量如实上报**：`usage` 按各协议语义回传（含 `prompt_tokens_details.cached_tokens` 与 Anthropic 的 `cache_read_input_tokens`；后者从 `input_tokens` 中扣除，避免重复计费错觉）。
- **推理内容透传**：思考过程以 `reasoning_content`（OpenAI）或 `thinking` 块（Anthropic）返回。
- **内置联网搜索**：Responses 的 `{"type": "web_search_preview"}` 与 Anthropic 的
  `{"type": "web_search_20250305", "name": "web_search"}` 会自动映射到上游的联网搜索，由 Studio
  在服务端执行；这类"服务端工具"不会被当成客户端函数下发，它触发的内部调用也不会漏给客户端。
- **多模态输入**：`image_url`（`data:` URI 或公网 URL）、OpenAI `file` 部件、Anthropic `image` /
  `document`（base64 / text / url）块都会自动上传为 Studio 资产并传给模型；同一份附件按内容哈希缓存，
  多轮对话不会重复上传。
- **多账号池**：轮询 + 冷却 + 自动续期；`Oasis-Token` 过期自动用 refresh token 换新，无需人工干预。
- **会话亲和**：OpenAI 客户端每轮都重发全量历史，网关只在尾部追加增量给上游，命中服务端上下文缓存
  （`cached_tokens` 会明显省钱省时）；历史分叉时自动开新会话并回放。
- **语音合成 / 识别**：`/v1/audio/speech` 直接返回音频字节（mp3 / wav / opus），OpenAI 的 voice 名会自动
  映射到 StepFun 的 37 个音色；`/v1/audio/transcriptions` 接收 multipart 音频并返回转写文本；
  `/v1/audio/voices` 可列出全部音色与试听地址。
- **Web 面板**：账号池、API Key、模型目录、按模型聚合的用量统计、请求流水（可导出 CSV）、
  Token 用量、上游限流状态，一屏可见；每 15 秒自动刷新（用户点击/请求在途时自动让路）。
  面板 API 也可脚本化：`GET /admin/api/state`、`/admin/api/logs`、`/admin/api/usage.csv`、
  `/admin/api/export`（均需 `X-Panel-Token`）。
- **零依赖**：只用 `http.server` / `urllib` / `ssl`，Python 3.9+ 即可运行，Docker 镜像也很小。

---

## 快速开始

```bash
# 方式 A：直接跑，无需安装
python step2api.py          # 或 python -m step2api
# Windows 双击 start-step2api.bat；Linux/macOS: ./start-step2api.sh

# 方式 B：装成命令（可选）
pip install .               # 开发时用 pip install -e .
step2api --port 8790
```

默认监听 <http://127.0.0.1:8790>，零运行时依赖（Python 3.9+）。启动时会后台预热（续期临近过期的账号、
拉取模型目录），首个请求不必等这些往返。打开面板用默认密码 `admin`
（首次登录后请到「设置」修改）。

### 1. 添加账号

在浏览器里登录 <https://studio.stepfun.com/>，打开 DevTools → Network，任意点开一个 `/api/` 请求，
复制请求头里的 **Cookie**，粘贴到面板「账号池 → 完整 Cookie」里即可。

需要的最小凭证是两项：

| Cookie | 说明 |
| --- | --- |
| `Oasis-Token` | `<access_jwt>...<refresh_jwt>`，中间是三个点 |
| `Oasis-Webid` | 设备标识，也会作为 `oasis-webid` 请求头发送 |

> 只粘贴 `Oasis-Token` 也可以，`Oasis-Webid` 会从 JWT 里的 `device_id` 自动解析。
> Access token 只有 30 分钟有效期，网关会用 refresh token 自动续期（refresh token 约 30 天，
> 且每次续期都会轮转、顺延 30 天）。后台每 6 小时还会主动续期一次，所以只要网关在跑，账号不会因为
> 闲置过期——面板「刷新令牌」列会显示还剩多少天。
> 若 refresh token 也过期，面板会显示「已过期，将自动续期」失败，重新粘贴一次 Cookie 即可。
> 添加时网关不只看续期是否成功：StepFun 的续期接口对伪造 Cookie 也会返回 200（换回一个访客会话），
> 所以还会打一次真实的鉴权接口（`QueryStudioRateLimit`）验证。粘贴同一账号的**另一个会话**时，
> 网关先用 access token 校验，只有它已失效才续期——无谓的续期会把浏览器里那份会话挤下线。凭证无效或只是访客会话时，新账号会被
> 自动**停用**而不是留在池里用坏凭证轮询——修正后在面板里重新粘贴（同一账号会更新原行，不会多出一条），
> 或点「启用」；上游临时 5xx 不会停用账号，稍后重试即可。

> 面板「导出备份」可下载完整 `accounts.json`（含凭证，注意保管）；换机器时直接覆盖 `--data-dir` 下的同名文件即可。

### 2. 生成 API Key

面板「API Key → 生成 Key」。**没有配置任何 Key 且未设置 `--api-key` 时，网关对本机开放**：同机网页（`localhost` / `127.0.0.1` 源，或与本请求同主机的来源）可以直接调用它；其它来源的浏览器请求会被**直接拒绝（403）**，不只是拿不到 CORS 头——只藏响应头的话，陌生网页仍能发出「简单请求」把你的额度烧掉。配置 Key 后，带 Key 的跨域客户端仍然可用。这种开放状态也不要暴露到公网。

### 3. 接入客户端

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8790/v1", api_key="sf-...")
resp = client.chat.completions.create(
    model="step-5-preview",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

Claude Code（Anthropic 协议）：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8790
export ANTHROPIC_AUTH_TOKEN=sf-...        # 面板生成的 Key；未生成可留任意值
export ANTHROPIC_MODEL=step-5-preview     # 也可不设，claude-* 名字会自动映射
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=64000   # 与上游一致；不设会按 200k 估算，可能发出超长请求
claude
```

> **实测（Claude Code v2.1.310）**：在隔离配置下（`CLAUDE_CONFIG_DIR` 指向空目录、
> `ANTHROPIC_API_KEY`、`CLAUDE_CODE_MAX_CONTEXT_TOKENS=64000`），让 Claude Code 用 Read 工具读取
> `hello.txt`，它正确回答出文件内容；网关流水记录两次成功轮次（19,718/345，缓存 18,944；
> 18,836/37，缓存 17,152，`finish_reason` 依次为 `tool_use` → `end_turn`），说明**系统提示、
> 工具调用、工具结果回传、最终回答**整条链路都走的是本网关，第二轮的缓存命中还证明会话被复用。
> 2026-10-25 改造后复测：Read 工具读取临时目录里的暗号 → 输出 `PINEAPPLE-42`，两轮 27,708/1,471（缓存 24,320）→ 24,813/106（缓存 21,248），`finish_reason` 依次 `tool_use` → `end_turn`。
>
> 注意：若本机 `~/.claude/settings.json` 的 `env` 里已经写了 `ANTHROPIC_BASE_URL`（例如指向某个
> 本地代理），**它会覆盖 shell 环境变量**，请求根本不会到达本网关——用 `CLAUDE_CONFIG_DIR` 指向
> 空目录隔离即可。模型名非内置时 Claude Code 会提示 `unrecognized_model`，属正常信息性提示。

Codex / 其它 Responses 客户端（`~/.codex/config.toml`）：

```toml
model = "step-5-preview"
model_provider = "step2api"

[model_providers.step2api]
name = "step2api"
base_url = "http://127.0.0.1:8790/v1"
env_key = "STEP2API_KEY"
wire_api = "responses"
```

其它 OpenAI 兼容客户端（Cline / Roo Code / Continue / Cherry Studio / Open WebUI / LobeChat / Chatbox …）：

| 字段 | 填什么 |
| --- | --- |
| Base URL | `http://<主机>:8790/v1`——只到 `/v1` 这一层，**不要**再拼 `/chat/completions` |
| API Key | 面板「API Key」生成的 `sf-…`；没生成 Key 时网关对本机开放，随便填一个非空值即可 |
| 模型 | `/v1/models` 里的 id（`step-5-preview` / `step-3.7-flash` / `step-3.5-flash-2603`）；填 `claude-*`、`gpt-*` 等外站名字也会映射到默认模型 |

这类客户端通常只发 `Authorization: Bearer`，网关同时接受 `x-api-key`（Anthropic 风格）。

---

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/chat/completions` | OpenAI Chat Completions，支持 `stream` |
| POST | `/v1/completions` | 旧版文本补全（`prompt` → `text`，支持 `stream`/`stop`；不支持 FIM `suffix`、`echo`、`logprobs`、`n>1`） |
| POST | `/v1/messages` | Anthropic Messages，支持 `stream` |
| POST | `/v1/messages/count_tokens` | 粗略 Token 估算（上游无公开 tokenizer） |
| POST | `/v1/responses` | OpenAI Responses API，支持 `stream` |
| POST | `/v1/audio/speech` | OpenAI 语音合成，返回音频字节 |
| POST | `/v1/audio/transcriptions` | OpenAI 语音识别（multipart `file`），返回转写文本 |
| GET | `/v1/audio/voices` | 上游全部 TTS 音色（含试听 URL） |
| GET | `/v1/models` | 模型目录（实时取自上游） |
| GET | `/v1/models/{id}` | 单个模型详情 |
| GET | `/health` `/ping` | 探活（不需要鉴权） |
| GET | `/` | Web 面板 |

### 请求扩展字段

| 字段 / 请求头 | 作用 |
| --- | --- |
| `step_mode` / `X-Step-Mode: agent` | 本请求走 Agent 工作区模式 |
| `reasoning_effort` | `low` / `medium` / `high`（`none` 关闭思考） |
| Anthropic `thinking.budget_tokens` | 映射成思考档位：< 4096 → `low`，< 16384 → `medium`，否则 `high`；`thinking.type: "disabled"` 关闭思考（上游没有 token 预算，档位是近似） |
| `step_thinking` / `X-Step-Thinking: off` | 强制开关思考 |
| `web_search: true` | 开启上游联网搜索 |
| `stream_options.include_usage` | 流式末尾返回 usage |
| `stop` | 字符串或数组，网关自己截断（上游无此参数），流式也保证不泄漏截断词 |
| `response_format` | `json_object` / `json_schema`：提示词约束 + 非流式结果抽取；流式只做提示词约束 |
| `max_output_tokens` | Responses API 的输出上限，等价于 `max_tokens` |
| `user` | 不同终端用户隔离会话亲和（同一 API Key 下互不干扰） |

附件输入只支持 `playground` 模式（Agent 工作区有自己的文件通道）；图片单张上限 10 MB、文档 20 MB，
**单条消息最多 8 个附件**——超过直接返回 **400**（不会静默丢弃，免得模型对着不完整的内容作答）。
客户端每轮重发整段历史时，同一个附件只计一次、也只上传一次（按内容哈希复用资产）。
单个 multipart 请求体（`/v1/audio/transcriptions` 等）上限 40 MB；超限直接 400，不会先读进内存。
从 URL 取附件是**边下边限流**的：声明长度超限立即拒绝，未声明长度的流一旦超过上限就掐断，不会出现「先读完 2 GB 再告诉客户端太大」。

### 不支持的参数

上游没有对应字段，网关不会假装支持：

| 参数 | 行为 |
| --- | --- |
| `n > 1` | 返回 400（上游只产出一个候选） |
| 旧版 `/v1/completions` 的 `suffix`（FIM）/ `echo` / `logprobs` | 返回 400 并说明；纯文本续写（含流式、`stop`）正常支持 |
| `previous_response_id` | 返回 400 并说明：请把完整历史放进 `input`（网关无响应存储） |
| 内联音频（`input_audio` / `audio` 部件） | 返回 400 并提示先用 `/v1/audio/transcriptions` 转写后再以文本发送（不会拿 `[audio]` 占位符糊弄模型） |
| `file_id` 引用（`/v1/files`） | 返回 400：网关没有文件存储，请内联 base64 `file_data` 或直接给 URL |
| `stop` / `stop_sequences` / `response_format` | 网关模拟（客户端侧截断 + 提示词约束），非原生 |
| `presence_penalty`、`logit_bias`、`top_k`、`seed`、`logprobs`、`parallel_tool_calls` | 忽略；模型/上游无此能力 |
| 语音合成 `speed` | 忽略 |
| `/v1/audio/translations` | 返回 400 并说明：上游 ASR 只转写、不翻译，请用 `/v1/audio/transcriptions` |
| Anthropic `cache_control` 标记 | 接受但忽略：上游自带提示缓存，命中情况会体现在 `usage.cache_read_input_tokens`（实测两段带标记的 system + 带标记的 tools 正常工作） |

附件（图片 / 文档）无法读取时（URL 打不开、base64 损坏、超过大小上限）返回 **400** 并说明原因，不会静默丢掉附件让模型空口作答。

被忽略的参数不会报错，避免打断只带了默认值的客户端；需要严格语义时请在网关外自行约束。

### 模型

模型列表不写死，启动后从 `GetStudioPlaygroundConfig` 拉取。当前为：

| id | 名称 | 多模态 | 思考档位 |
| --- | --- | --- | --- |
| `step-5-preview` | Step 5 Preview | ✓ | low / medium / high |
| `step-3.7-flash` | Step 3.7 Flash | ✓ | low / medium / high |
| `step-3.5-flash-2603` | Step 3.5 Flash | — | low / high |

支持 `stepfun/` 前缀写法（如 `stepfun/step-5-preview`）。

**外部模型名自动映射**：`claude-sonnet-4-5`、`gpt-5-codex`、`o3-mini` 这类外部家族的名字会自动落到上游默认模型
（`step-5-preview`），这样 Claude Code / Codex 不用改配置就能连；其它拼错的名字仍返回 400，避免把笔误当成功。

---

## 配置

优先级：命令行 > 环境变量 > 默认值。环境变量写错（例如 `STEP_UPSTREAM_TIMEOUT=600s`）会回退到默认值，不会让进程带着 traceback 退出。

| 命令行 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `--host` | `STEP_HOST` | `127.0.0.1` | 监听地址；对外服务改 `0.0.0.0` |
| `--port` | `STEP_PORT` | `8790` | 监听端口 |
| `--data-dir` | `STEP_DATA_DIR` | `accounts` | 账号 / 设置 / 用量落盘目录 |
| `--api-key` | `STEP_API_KEY` | 空 | 单 Key 模式（配置面板 Key 后失效） |
| `--panel-password` | `STEP_PANEL_PASSWORD` | `admin` | 面板密码；在面板里改过之后以面板保存的哈希为准，此参数不再生效 |
| `--mode` | `STEP_DEFAULT_MODE` | `playground` | 默认上游模式 |
| — | `STEP_UPSTREAM_TIMEOUT` | `600` | 上游读超时（秒） |
| — | `STEP_CONNECT_TIMEOUT` | `30` | 连接与单次 RPC 的超时（秒）；上游“黑洞”时单请求总等待约 `× (STEP_MAX_RETRIES + 1)` |
| — | `STEP_REFRESH_MARGIN` | `180` | access token 到期前多久提前续期（秒） |
| — | `STEP_SSE_HEARTBEAT` | `5` | 客户端 SSE 心跳间隔，`0` 关闭 |
| — | `STEP_MAX_RETRIES` | `3` | 单请求最多换几次账号 |
| — | `STEP_MAX_CONCURRENCY` | `8` | 同时进行中的上游轮次上限（超出排队，60s 超时返回 429） |
| — | `STEP_COOLDOWN_WAIT` | `10` | 所有账号短暂冷却时最多等多久再重试（秒） |
| — | `STEP_SESSION_TTL` | `1800` | 会话亲和缓存保留时长（秒） |
| — | `STEP_KEEPALIVE` | `21600` | 后台续期间隔（秒，`0` 关闭）：每轮把临近过期的账号续期一次，闲置也不会掉线 |
| — | `STEP_USAGE_MAX_BYTES` | `8388608` | 请求流水文件上限，超过后轮转为 `.1` |
| — | `STEP_BASE_URL` | `https://studio.stepfun.com` | 上游地址（自建镜像/联调时改它；每次请求实时读取） |
| — | `STEP_EXPOSE_REASONING` | `1` | 是否返回思考内容 |
| — | `STEP_ALLOW_PRIVATE_FETCH` | 空 | 允许拉取内网/回环地址的图片与文档 URL；默认**拒绝**，防止客户端把网关当成访问本机服务的跳板（附件放内网时再打开） |
| — | `STEP_USAGE_LOG` | `1` | 是否记录请求流水 |
| — | `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | 空 | 出站代理（仅这些变量，不读系统代理） |

数据文件（`--data-dir` 下）：

- `accounts.json` — 账号、API Key、面板设置（含 Cookie，注意权限，不要提交到仓库）；
- `usage.jsonl` — 追加式请求流水。

---

## Docker

```bash
docker compose up -d --build
# Base URL: http://127.0.0.1:8790/v1
```

> 说明：Dockerfile 只是 `COPY` + `CMD`（无构建期依赖、无 `pip install`）。本机没有 Docker，
> **未做真实镜像构建**；但已按镜像内的文件集合（`step2api/`、`step2api.py`、`README.md`）在空目录里
> 用 Dockerfile 的环境变量（`STEP_HOST=0.0.0.0`、`STEP_DATA_DIR=/data`）起过一次：`/ping` 返回 `pong`、
> `/health` 正常。未覆盖的是 alpine + Python 3.12 组合与 `wget` 健康检查本身。

`accounts/` 与 `usage/` 需要挂载持久化（compose 已把 `./accounts` 挂到 `/data`），否则容器重建后账号丢失。
容器内进程以 root 运行，`docker-compose.yml` 只把端口发布到 `127.0.0.1`；若要对外，请同时设置
`STEP_API_KEY`（或先在面板生成 Key）并把发布地址改成 `0.0.0.0`。

---

## 停机行为

`Ctrl+C` / `SIGTERM` 后先停止接受新连接，再**最多等 10 秒**让在途请求跑完（日志会打印
`drained in-flight requests in x.xs`），然后关闭监听。10 秒内结束的流式请求会完整收尾；
仍在生成的长流会被断开（客户端按 SSE 断流处理），需要「一条都不许断」请等流结束后再停。

## 截断语义

输出被 token 上限截断时，各协议按自己的规范如实上报，不会假装正常结束：

- OpenAI：`finish_reason: "length"`
- Anthropic：`stop_reason: "max_tokens"`
- Responses：`status: "incomplete"` + `incomplete_details.reason: "max_output_tokens"`

上游在 `done` 之前断流（网络中断、连接被中途掐断）按**失败**上报，不伪装成正常结束：
流式客户端收到一个 `error` 帧后结束，不会补一个假的 `finish_reason: "stop"`；非流式请求返回 502。
上游回 200 但**一个事件都没有**也算断流（不会变成「空答案 + 200」）。重试用尽且没有别的账号可换时，
返回的同样是最初的上游错误（502），不是空成功。没有明确状态码的网关/上游错误按 502 返回（请求本身有问题才是 400；无可用账号 503、并发闸门 429）。
语音合成/识别同样要求流以 `done` 收尾，缺了就是 transient 失败，而不是「没有音频」或「半截转写」当作成功。
重试策略：上游 200 空流在开流阶段就会重试（还没有任何输出）。已经收到事件之后，**非流式**会缓冲整段事件，
断流时在新会话里重放一次（旧会话直接丢弃，不会把同一轮用户消息追加两次），再断就返回 502；
**流式**无法重放（字节已经发给客户端），只发一个 `error` 帧收尾。

## 验证状态

| 能力 | 验证方式 |
| --- | --- |
| chat 非流式 / 流式 / usage | 真实上游 + OpenAI SDK 2.44 |
| 工具调用（chat / messages / responses 三协议，均含「调用 → 回传结果 → 最终回答」二轮；chat 与 messages 另有**流式增量拼装**检查，交给官方 SDK 的累加器） | 真实上游 + 官方 SDK（`sdk_check.py` 的 `* tool round trip`），e2e 覆盖事件序列与参数增量 |
| 视觉 / 文档输入 | 官方 SDK 可复现检查：程序生成的纯红 PNG → 回答「红色」；base64 `text/plain` 文档含暗号 `ZQ7-ALPHA-9` → 原样读出 |
| 视觉输入（官方 SDK，程序生成的纯色 PNG） | 真实上游：模型回答「红色」，证明上传→资产→上游识别全链路 |
| 语音合成 / 识别 | 真实上游 + 官方 SDK（MP3 `ID3` 魔数、回读转写文本）；OpenAI 模型名实跑：`tts-1` 合成 17KB MP3 → `whisper-1` 转写回「你好。」，响应头 `X-Step-Model` 为 `stepaudio-2.5-tts` / `stepaudio-2.5-asr` |
| Responses 流式工具调用（官方 SDK 的事件累加器；工具先于文本时索引仍从 0 开始） | 真实上游 + 官方 SDK：`response.output_item.done` 产出 `function_call`，带 `function_call_output` 的第二轮得到最终答案 |
| Responses 结构化输出（`text.format` + `json_schema`，含非流式围栏兜底） | 真实上游 + 官方 SDK：返回可直接 `json.loads` 且符合 schema 的对象 |
| 持续负载 / 内存 | 离线 6 轮 × 120 请求（并发 6，流式/非流式/工具混合）：0 失败、`inflight` 归零；RSS 呈锯齿（132→228→322→89→178→272 MB），手动 `gc.collect()` 后从 322MB 回落到 47MB —— 增长是待回收的循环垃圾，不是泄漏 |
| 大上下文 + 多工具（模拟 Claude Code：~18k token 系统提示、5 个工具、两轮工具闭环） | 真实上游：turn1 输出 `tool_use`，turn2 带 `tool_result` 得到最终答案；`cache_read_input_tokens` 17280 正常回传 |
| Claude Code 实机 | v2.1.310 隔离配置后接入：Read 工具调用 + 最终回答成功，网关侧记录两轮（19,718/345，缓存 18,944；18,836/37，缓存 17,152），流水 `finish_reason` 依次 `tool_use` → `end_turn` |
| 客户端模型名兼容 | 官方 SDK 实测：`claude-sonnet-4-5-*`、`gpt-5`、`o3-mini`、`codex-mini-latest` 都映射到上游默认模型并正常回答 |
| 内置联网搜索（两种协议的 server tool 写法） | 真实上游：问到当天新闻，返回带当日事件的具体答案，且响应里没有伪造的 `tool_call` |
| 多账号：轮询 / 会话绑定账号 / 5xx 故障转移 / 冷却等待 / 无可用账号返回 503 | fake 上游 e2e（真实场景需要第二个账号） |
| 并发闸门 / 泄漏 | `soak.py` 16 请求 × 并发 6 + 2 次主动断连，`inflight`/线程数归零 |
| 连接复用 | 同一 HTTP 连接连续 3 次流式 + 1 次非流式（`will_close=false`），贴合 httpx/OpenAI SDK 的连接池 |
| 面板（登录/登出/添加/测试/单账号续期/全部续期/启停/Key 生命周期/导出/CSV） | Chrome DevTools 实机逐按钮点击 + e2e 路由测试 + 面板 JS 语法检查（`node --check`，无 node 时跳过）。单账号「续期」曾因路径下标错一位而恒返回 404，已修复并加回归测试 |
| 授权与存储加固 | 禁用最后一个 Key 后仍要求 Key；`accounts.json` 损坏会被改名保留而非清空；Windows 文件占用时落盘自动重试；关闭 panel 会话随密码变更失效；`Transfer-Encoding: chunked` 明确 400 |
| 请求追踪 | 每个响应带 `X-Request-Id`（JSON/SSE/音频），与 `usage.jsonl` 的 `request_id` 一致，e2e 校验格式与唯一性 |
| 浏览器侧暴露面 | 无 Key 时 `Origin` 非 loopback/同主机的请求直接 **403**（不是只藏 CORS 头），e2e 验证被拒请求未触达上游；有 Key（面板 Key 或 `--api-key`／`STEP_API_KEY`）时回显来源，两条准入判断用同一个"是否配了 Key"的判据（曾出现 CORS 放行、请求本身却 403 的分裂）。`/admin` 永不发 CORS；面板 `X-Frame-Options: DENY` + CSP，全局 `nosniff`，`Server` 头只报 step2api 版本（不暴露 Python 版本） |
| 面板 XSS / 敏感数据 | Node DOM shim 跑真实 `render()`，注入 `<img onerror>`、`" onmouseover=` 等载荷后 innerHTML 中无标签、无注入属性；`/admin/api/state` 不再返回面板密码哈希、不再在请求线程里访问上游（15s 轮询不会压在卡住的上游上）；面板状态不再写 localStorage；面板响应带 CSP（`default-src 'self'`、`connect-src 'self'`、`object-src 'none'`、`frame-ancestors 'none'`）——即使有脚本注入也读不到也发不出去；实机登录/渲染/登出无 CSP 报错 |
| 配置容错 | `STEP_PORT=70000`、`--port -5`、`STEP_DATA_DIR=` / `--data-dir "  "`、空面板密码均回落到默认值；`--mode` 显式指定时不再被面板里保存的默认模式覆盖（CLI > env > 面板） |
| 存储形状容错 | `{"accounts": 5}`、`{"settings": "oops"}`、顶层数组等「合法 JSON、错误形状」与截断文件一样改名保留，进程不再启动即崩；手改出来的字符串统计（`tokens_in: "<img…>"`）被强制转成数字 |
| 重复启动 | 同端口第二个实例 exit 2；换端口但同 `--data-dir` 也 exit 2（`gateway.pid`，陈旧 pid 自动忽略）；`--doctor` 同样据此拒绝续期 |
| 附件 URL 防 SSRF | 回环 / 私网 / 链路本地 / 云元数据地址一律 400；`STEP_ALLOW_PRIVATE_FETCH=1` 可放行内网资源 |
| 客户端中途断开 | 真实上游：流式断开后 1s 内探测到（select + MSG_PEEK，不消耗请求数据），立即关闭上游流、释放并发许可，流水记 `client_closed`；非流式在收集阶段同样探测，且**不会**为已离开的客户端重放（e2e：`DRIP` 慢流 + 半关闭 socket，只发生一次上游生成、`inflight` 归零） |
| 优雅停机 | 真实 SIGINT 探针：`serve_forever` 1s 内退出并 drain（旧实现会自锁死） |
| 错误体协议化 | `/v1/messages` 返回 Anthropic `{"type":"error",...}` 信封，其余端点返回 OpenAI 形状；e2e + 官方 SDK 校验 |
| 打包 | `pip wheel .` 构建后安装到干净 venv：`step2api -V` 与 `python -m step2api -V` 均可用，包内包含 `py.typed` 与控制台入口 |
| 部署后自检 `scripts/smoke.py` | 真实网关实测：7 项全过（9.5s）；对不可达端口给出 4 项 ✗ 并 `exit 1` |
| 上游自检 `--doctor` | 真实上游全绿：续期并持久化、配额、模型目录、非流式/流式/**agent 模式**；`--media` 追加 TTS→上传→ASR 实跑（实测 52,902 字节 MP3 → 转写「你好，这是 Step 2 API 自检。」），同时验证两条媒体流都以 `done` 收尾；网关在跑时**拒绝执行**（exit 1、不续期），`--force` 可跳过 |
| 启动脚本（4 个：普通/局域网 × bat/sh） | 逐个实跑：`.sh` 在 Git Bash 下挑到可用解释器、`.bat` 以 CRLF 保存并由 `chcp 65001` 正确显示中文；局域网模式无 Key 401、带 Key 200 |
| 上游中途断流（`done` 之前就结束 / 200 但零事件） | fake 上游 `CUT` / `CUT_ONCE` / `EMPTY` 模式：流式客户端只收到 1 个 `error` 帧 + `[DONE]`，不补 `stop`；非流式与空流返回 502 而不是空答案；非流式断流会在新会话里重放一次（第二次仍断则 502），流式不重放；单账号耗尽重试时返回最初的上游错误（不再 `first=None` 静默成功）。去掉终止符检查或 `connected` 判据时对应用例转红（对质）；真实上游两种对话面都确认正常流以 `final` → `done` 收尾，TTS/ASR 截断单测同样在去掉检查时转红 |
| Python 版本 | 测试套件在 CPython **3.9 / 3.10 / 3.11 / 3.12 / 3.13 / 3.14** 上逐一跑过，均全绿（353 项）；另有测试用 `ast.parse(feature_version=(3,9))` 把源码钉在 3.9 语法上，并静态检查运行时只导入标准库 |
| Docker | 未实测（本机无 Docker） |

## 自检

```bash
python scripts/smoke.py              # 部署后自检：只用标准库，验证"客户端视角"的 HTTP 面
python step2api.py --doctor          # 上游侧自检（不需要装任何依赖）
python step2api.py --doctor --media  # 再加 TTS→上传→ASR 实跑（会消耗少量额度）
python scripts/sdk_check.py          # 客户端侧自检（需 pip install openai anthropic）
python scripts/soak.py --count 16 --concurrency 6   # 短时压测 + 泄漏检查（真实上游）
python scripts/soak.py --key sk-xxx                  # 网关配了 API Key 时带上
```

- `smoke.py`：**零依赖**，适合刚 `docker compose up` 或换机器部署后跑一次：探活、模型目录、
  chat（非流式/流式）、`/v1/messages`、`/v1/responses`，7 项全过才返回 0；`--key` 传 API Key、
  `--timeout` 可用于快速判断"端口通不通"；
- `--doctor`：账号续期、配额、模型目录、非流式/流式/agent 对话，直连上游，给出失败原因；agent 通道不可用时只给警告（playground 不受影响）。
  `--media` 额外做一次真实的 TTS→上传→ASR 往返（默认关闭，因为会消耗额度）。
  **跑之前先停掉正在运行的网关**：doctor 会自己读 `accounts.json` 并续期，而 Studio 每次续期都会轮换
  refresh token——两个进程同时持有一份账号时会互相把对方的 refresh token 作废。检测到同端口已有网关时
  doctor 会直接退出（exit 1，不做任何续期）；确认数据目录不同、不会冲突时可加 `--force` 跳过该检查；
- `sdk_check.py`：用**官方 SDK**（OpenAI + Anthropic）跑 25 项，重点是"客户端真能消费"：
  chat（流式 / 工具增量拼装 / 推理透传 / 旧版补全 / 视觉）、messages（流式 / 思考块 / 工具增量拼装 /
  `stop_sequences` / 文档输入 / `cache_control`）、responses（结构化输出 / 流式工具）、
  语音合成+识别、`count_tokens` 本地估算、错误体协议化、models、**外站模型名映射**（`claude-*` / `gpt-*` → 上游默认模型）。
  单项失败会自动重试一次（模型行为与网络都会抖），只有两次都失败才算失败。
- `soak.py`：混合并发流量 + 主动中途断开，随后根据 `/health` 的 `inflight`、`threads` 判定
  是否有并发许可或线程泄漏。实测 `16 请求 / 并发 6 / 10.1s / 0 失败 / inflight 归零 / 线程 4→4`。

实测输出：

```
✓ openai chat        … | finish=stop
✓ openai stream      chunks=21 text='1、2、3、4、5。' tokens=1501
✓ openai tools       finish=tool_calls call=('get_weather', '{"city": "北京"}')
✓ openai speech+asr  bytes=49066 magic=b'ID3' transcript='这是通过官方 SDK 合成的语音。'
✓ anthropic tools    stop=tool_use blocks=[('text', None), ('tool_use', 'get_weather')]
```

## 运维要点（长期运行时看这里）

（上游不可达时的等待时间与调参见「常见问题」。）

**`/health` 怎么读**
`healthy` 是"账号可用"（有 token、未冷却、未停用），**不代表上游可达**。上游是否正常要看请求错误体，
或面板里的账号状态。`inflight` 长期不为 0 → 有请求卡住；`threads` 随时间单调上涨 → 有泄漏；
`sessions` 是会话亲和缓存条目数（上限 1024，闲置 30 分钟淘汰）。

**内存怎么看**
RSS 呈锯齿：请求处理会产生引用环（handler↔server、闭包），靠循环回收器回收，不保证立即释放。
实测 6×120 请求（并发 6）时 132→228→322→**89**→178→272 MB，手动 `gc.collect()` 可让 322MB 立刻回落到
47MB，且活对象只有会话对象。**看到高 RSS 先用 `gc.collect()` 验证，不要直接判定泄漏。**
反过来，Windows 在内存压力下会裁剪空闲进程的工作集，`/health` 的 `rss_mb` 可能突然掉到几 MB，
随后随请求回升——同样是正常现象，不代表进程重启（看 `uptime` 与 `gateway.pid` 即可确认）。

**`accounts.json` 损坏**
启动时无法解析会把原文件改名为 `accounts.json.corrupt-<时间戳>` 并保留，然后以空池启动——
**不会**用空数据覆盖你的账号。把备份文件改回来即可恢复。面板的"导出备份"按钮可以随时存档。

**端口被占用 / 不小心起了两份**
第二个实例会直接退出（exit 2）并提示端口已被占用，而不是和第一个实例共用同一个端口——两份网关
共用 `accounts.json` 会互相轮换同一个 refresh token，先坏掉的会是账号本身。Windows 上
`SO_REUSEADDR` 原本允许这种"抢端口"，这里显式关掉了。即使换了 `--port`，只要 `--data-dir` 相同，
启动时也会被 `gateway.pid` 拦住（文件里的 pid 仍存活就拒绝启动，进程异常退出留下的陈旧 pid 会自动忽略）；
`--doctor` 也据此拒绝续期。

**用量流水**
写不进或读不出（磁盘满、权限）时，stderr 会各打印一次原因；请求本身不受影响。面板显示"暂无请求"
但请求数在涨时，先看服务端日志。流水每条都记 `finish_reason`（按协议原生词汇：`stop`/`length`/`tool_calls`、`end_turn`/`tool_use`、
`completed`/`incomplete`）、`client`（请求的 User-Agent）和 `tenant`（API Key 的**名字-短哈希**，不是 Key 本身——同名 Key 也仍然互相隔离——导出的 CSV 可能被转发，凭证不进入流水）；CSV 里同样有这些列，外加 `request_id` 与 `replayed`。
面板里看到 `claude-cli/2.1.310` 才说明 Claude Code 真的连的是本网关——如果那里显示的是别的
User-Agent 或根本没有记录，说明客户端连的是 `~/.claude/settings.json` 里配置的另一个代理。
每个响应（JSON / SSE / 音频 / 面板）都带 `X-Request-Id`（`req_` + 16 位十六进制），同一请求在 `usage.jsonl` 里的 `request_id` 与它一致——用户报问题时直接要这个 id 就能定位到那一行。客户端自带的 `X-Request-Id` 一律忽略（网关自己生成，避免日志被伪造或注入）。
非流式请求在上游断流后重放过一次时，该行会多一个 `replayed: true`——它不是失败标记，只说明这次答案多花了一轮上游生成（流式无法重放，不会出现该字段）。
CSV 里客户端可控的字段（如 User-Agent）若以 `=`、`+`、`-`、`@` 开头，会加一个前导 `'`，
防止 Excel/Sheets 把它当公式执行。

## 常见问题

**一直显示「没有可用账号 / 503」**
账号被标记冷却（上游报错后会冷却 15–30 秒），面板点「测试」立即解除；仍失败则重新粘贴 Cookie。
状态是「停用」则说明凭证无效、或只是个访客会话（见「添加账号」）；重新粘贴 Cookie 会把该行更新并重新启用，
面板点「测试」通过也会自动解除停用。`429` / `503` 响应会带 `Retry-After`（秒，默认等于
`STEP_COOLDOWN_WAIT`），OpenAI / Anthropic 官方 SDK 会据此自动退避。

**浏览器里的网页直接调用网关时报 CORS 错误 / 403**
没有配置 Key 的网关只接受 `localhost` / `127.0.0.1` / 同主机来源：别的来源不只是拿不到 CORS 头，
请求本身会返回 403（仅藏响应头挡不住「简单请求」，额度照样会被消耗）。两条路：在面板生成 API Key
并在请求里带上（有 Key 时网关会回显来源），或让后端转发（服务端调用不受 CORS 限制）。

**返回内容为空、只有思考**
模型把内容全放在思考里且被 `max_tokens` 截断。提高 `max_tokens`，或 `reasoning_effort: "none"`。
少数情况下上游会返回 **0 个输出 token 的空回复**（`usage.output_tokens` 为 0、状态仍是 completed），
这是上游的偶发行为，重试即可；结构化输出最容易被它命中，`sdk_check.py` 因此对该项多给一次机会。

**多轮对话后模型「忘记」前文**
网关按「历史前缀」复用上游会话。若客户端改写了历史（滑动窗口、编辑消息），会自动新开会话并回放记录，
但会损失一部分上下文缓存收益。

**上游网络不通时，一个请求要等很久**
最坏等待 ≈ `STEP_CONNECT_TIMEOUT × (STEP_MAX_RETRIES + 1)`（默认 30s × 4 = 2 分钟），因为 SYN 被
丢包时 TCP 不会立刻失败。想要快速失败：`STEP_CONNECT_TIMEOUT=2 STEP_MAX_RETRIES=1`，实测评测中
不可达上游 5 秒内返回 503，错误体里带真实原因；期间 `/health` 始终可用。

**上游报 `CENSORED` / 内容审核**
这是确定性的拒绝，重试无效，需要修改输入内容。

**Agent 模式很慢、耗 token 多**
Agent 有固定的系统提示（约 4k tokens）与工具循环，适合「做事」而非闲聊。日常对话用默认的 `playground`。

---

## 开发

```bash
# 全部测试（无需网络，自带 fake 上游）
python -m unittest discover -s tests -v

# 单个文件
python -m unittest tests.test_translate -v
python -m unittest tests.test_e2e.GatewayE2E.test_stream -v
```

上游协议细节（鉴权、两种会话通道、SSE 事件表、错误码）见 [CLAUDE.md](CLAUDE.md)；
贡献流程、合入规则与 AI 代码政策见 [CONTRIBUTING.md](CONTRIBUTING.md)。

### 语音合成示例

```python
audio = client.audio.speech.create(
    model="stepaudio-2.5-tts",
    voice="nova",              # 也接受 step_f06 / 钟离 / xiaoyue 等原生音色
    input="你好，这是 step2api。",
    response_format="mp3",
)
audio.write_to_file("hello.mp3")
```

`speed` 上游没有对应参数，会被忽略；`aac` / `flac` / `pcm` 会回落到 `mp3`（`pcm` 回落 `wav`）。
响应头 `X-Step-Voice` / `X-Step-Model` / `X-Step-Format` 会告诉你最终实际使用的音色、模型与格式。

OpenAI SDK 自带的音频模型名会被映射到账号实际可用的模型：`tts-1` / `tts-1-hd` /
`gpt-4o-mini-tts` / `gpt-4o-tts` → 上游 TTS 默认模型（当前 `stepaudio-2.5-tts`）；
`whisper-1` / `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` → 上游 ASR 默认模型
（当前 `stepaudio-2.5-asr`）。其他未知名字原样透传，由上游如实报 `MODEL_ACCESS_DENIED`，网关按 **400** 返回，并且不会因为一个客户端拼错模型名就冷却账号（不影响其他人的请求）。
如果名字确实在上游目录里、只是**某一个账号没有权限**，网关会换下一个候选账号再试（目录本来就是某个账号返回的，权限可能不同）；所有候选都拒绝时返回上游的 400，同样不冷却任何账号。

### 语音识别示例

```python
with open("hello.mp3", "rb") as fh:
    result = client.audio.transcriptions.create(model="stepaudio-2.5-asr", file=fh)
print(result.text)
```

`model` 也可以直接写 `whisper-1`（OpenAI SDK 的默认名），网关会映射到 `stepaudio-2.5-asr`；
返回头 `X-Step-Model` 会给出实际使用的模型。
`language` 参数上游不接收（模型自动判别语种）。`response_format`：`json`（默认）与 `text` 完整支持；`verbose_json` 返回 `text` + `task`，但 `duration`/`segments` 为 `null`/空（上游不给时间戳）；`srt`/`vtt` 需要时间戳，直接返回 400 并说明原因，不会假装成纯文本。

### JSON 模式说明

Studio 没有原生 JSON mode，网关分两层保证：

1. 系统提示里加入强约束（"整段回复会被 `json.loads()` 解析"）；带 `json_schema` 时把
   schema 原文一并写进提示；
2. **非流式**再兜底抽取第一个合法 JSON 对象（去 Markdown 围栏、去前后散文）。

三种写法都支持：OpenAI chat 的 `response_format`（`json_object` / `json_schema`）、
Responses 的 `text.format`（同上两种 `type`）。Anthropic 协议没有对应字段。

流式模式只有第 1 层——文本是边生成边下发的，无法回溯改写。要求严格 JSON 时用非流式。

## 范围说明

已支持：文本对话、流式、思考内容、联网搜索、工具调用（含流式增量与二轮回传）、多轮会话、
结构化输出、图片与文档输入、多账号故障转移、三种协议入口 + 旧版 `/v1/completions`、语音合成与识别。
未支持：

- **图片生成 / 编辑**：上游当前对该账号直接返回
  `400 image generation is temporarily unavailable`，网关没有接线（接上也是同样的错）；
- **Embeddings / Moderations**：上游没有对应模型或接口；`/v1/embeddings`、`/v1/moderations`
  会返回 400 并在错误体里说明原因，不会给出无法使用的假向量；
- **Agent 模式的附件**：agent 工作区只有服务端 `file_paths`，没有客户端上传槽位，因此
  `step_mode: agent` 的请求**带图片/文档会直接返回 400**（不会静默丢掉输入让模型空口作答）；
  需要视觉/文档输入请用 playground 模式（不传 `step_mode`）；
- **站点部署 / 分享**：未接线。

这些能力在 `/v1/models` 里可见（`chat: false`），但不能用于 `/v1/chat/completions`。

## 项目政策

- **只接受 Pull Request**：Issues 与 Discussions 已关闭，缺陷与需求都以 PR 形式提出（见 [CONTRIBUTING.md](CONTRIBUTING.md)）。
- **`main` 受保护**：所有改动（包括维护者自己的）都必须经过 PR，CI 全绿才能合入，禁止直接 push 与强推。
- **AI 生成代码必须随附单测情况**：PR 模板要求声明 AI 生成范围、对应测试与实跑结果；
  `policy / pr-metadata` 工作流会在缺失时红掉 PR。
- **安全漏洞**请走私密渠道：[Security Policy](SECURITY.md)，不要用 PR 公开利用细节。

## 免责声明

仅供个人学习与自用。请遵守 StepFun 的服务条款；账号凭证由使用者自行保管，本项目不做任何凭证上传。

License: [Unlicense](LICENSE)（公有领域，即"无许可证"：随便用、随便改、随便商用，不附带任何担保）。
