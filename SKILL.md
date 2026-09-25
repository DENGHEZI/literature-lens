---
name: literature-lens
description: 文献透镜 —— 学术文献翻译专家智能体：将科研 PDF 变成含中英对照、术语、创新点、原生 PDF 查阅、划选翻译与基于证据的 AI 问题导图的交互式网页。用于翻译论文、精读文献或基于当前文献回答问题；支持用户自备的 OpenAI 兼容、Anthropic、Gemini 或 Ollama API。
---

# 文献透镜 LiteratureLens · 学术文献翻译专家

把一批科研 PDF 变成**一个可交互的阅读工作台**：左边选文献，中间看中英对照译文，右边看创新点卡片，
**点击创新点里的知识点标签，右侧滑出深度讲解面板**（定义 / 原理 / 公式 / 易错点 / 关联概念）。
阅读体验对齐小绿鲸英文文献阅读器的核心形态：划词/悬浮翻译、术语高亮与筛选、笔记大纲与思维导图、护眼与全屏。

本 skill 的智能体身份是**忠实的学术文献翻译与证据解读专家**：翻译保持术语、公式、引文与不确定性；
解释、创新点和导图只依据当前论文，无法从原文支持时明确写「原文未提及」，不把合理推断说成作者结论。

## 新版交互能力：上传 PDF 与问题导图

- 网页工具栏的「导入 PDF / 打开 PDF」打开**小绿鲸风格的三段式 PDF 阅读器**:
  **左侧已上传列表**(每项显示文件名/大小/同步状态/时间,可点选、可移除)+ **主区大拖拽区**(点击或拖拽上传,支持多选)+ **顶部工具条**(‹ › 切换上一篇/下一篇、下载、新窗口打开)。
- 上传后**先用浏览器本地对象(Blob URL)直接预览**,所以即使用户双击 HTML、尚未启动服务也能看图;服务可用时才额外把文件同步到本机 `uploads` 目录并校验 `%PDF-` 文件头,同步成功后在列表里标「已同步」。
- PDF 交给浏览器内置解释器展示,保留原生页码、搜索、缩放和下载能力。
- 页面控件带双语悬停译释;已处理文献仍保留英文段落悬浮中文译文与原文页划选翻译。
- 「AI 问题导图」会读取当前选中文献与用户问题,调用 `mindmap` 角色的 API,返回带"原文依据"的 SVG 导图。用户可以用 Ctrl/Cmd+Enter 提交。
- 这几项交互需要通过 `serve.py` 打开的本机页面(或双击 HTML + 已启动服务,靠 `apiUrl()` 跨域);直接双击静态 HTML 且未启动服务时仍可离线阅读译文/创新点/知识点,PDF 也能本地预览,但在线翻译、AI 对话、配置保存不可用。

### PDF 阅读器的三个坑(改前必读)

1. **拖拽上传必须阻止默认行为**:window 上不拦 `dragover/drop`,浏览器会直接打开被拖入的 PDF 并丢弃当前页面。代码里对 window 全局拦了这两个事件,对 drop zone 又单独拦一次。
2. **`input[type=file]` 选同一文件第二次不触发 `change`**:每次处理完必须 `e.target.value = ''` 清空,否则用户重选同一份 PDF 没反应。
3. **服务端 id 与本地占位 id**:上传时先用 `local-xxx` 占位 id 立即渲染,同步成功后换成服务端 32 位 hex id;换 id 时要把 `pdfServerUrls` 的映射一起迁移,否则点列表项会加载不到图。

## 用户自备 API 与双算力

用户可在页面顶部「我的 API 设置」直接接入自己的 OpenAI 兼容 / Anthropic / Gemini / Ollama API;
页面会把配置写入本机 `config.user.json`,**不把密钥嵌进网页或回传**。
也可复制 `config.user.example.json` 为 `config.user.json` 后手动填写。示例分离两种计算能力:

| 角色 | 用途 | 推荐配置 |
|---|---|---|
| `translate` | 长文块翻译、划词在线翻译 | 长上下文、成本稳定的翻译 API |
| `analyze` / `mindmap` | 创新点、知识讲解、问题理解与证据导图 | JSON 稳定、推理较强的 API |
| `chat` | AI 助手多轮对话(右栏新 tab) | 文本能力强、上下文 ≥8K 的 API |

### 接入流程(90% 用户推荐走 1-2-3 步)

页面顶部的「我的 API 设置」对话框已重写为三模式:

```
┌─ 单 API 模式(推荐)───────────────────────────────────┐
│ [DeepSeek] [智谱 GLM] [Kimi] [OpenAI] [Claude] [Gemini] [通义] ... │
│  ↓ 选一个预设                                          │
│  API Key:[sk-...]                                      │
│  [测试连接]  [保存并启用]  ← 不填地址/模型也能用          │
│  (高级: 自定义地址/模型/协议,默认折叠)                   │
└────────────────────────────────────────────────────────┘
┌─ 双 API 模式(翻译 / 分析分开)─────────────────────────┐
│ 工具1 学术翻译:base_url / model / key                 │
│ 工具2 科研助手:base_url / model / key                 │
│ 工具2 留空 → 自动复用工具1                              │
└────────────────────────────────────────────────────────┘
┌─ 高级/自定义(暂存原表单,后端兼容)──────────────────────┐
│ translation + reasoning 双 provider 载荷(原结构)        │
└────────────────────────────────────────────────────────┘
```

### 11 个内置预设(后端 `serve.API_PRESETS`,前端通过 `__PRESETS_JSON__` 注入)

| 芯片 | base_url | 默认 model | protocol |
|---|---|---|---|
| DeepSeek | api.deepseek.com/v1 | deepseek-chat | openai |
| 智谱 GLM | open.bigmodel.cn/api/paas/v4 | glm-4-plus | openai |
| Kimi | api.moonshot.cn/v1 | moonshot-v1-128k | openai |
| OpenAI | api.openai.com/v1 | gpt-4o-mini | openai |
| Anthropic Claude | api.anthropic.com/v1 | claude-sonnet-4-20250514 | anthropic |
| Gemini | generativelanguage.googleapis.com/v1beta | gemini-2.0-flash | gemini |
| 通义千问 | dashscope.aliyuncs.com/compatible-mode/v1 | qwen-plus | openai |
| 豆包 | ark.cn-beijing.volces.com/api/v3 | doubao-1-5-pro-32k-250115 | openai |
| 硅基流动 | api.siliconflow.cn/v1 | Qwen/Qwen2.5-72B-Instruct | openai |
| OpenRouter | openrouter.ai/api/v1 | openai/gpt-4o-mini | openai |
| 自定义 / 其他 | (空) | (空) | openai |

> 自定义不会写进预设表,完全靠用户在"高级选项"里填写。
> **Ollama** 协议(本地模型)虽已实现,但**默认不暴露为预设**——遵守用户既有要求"用接入 AGENT 里的模型,不用本地模型"。

### 测试连接(`/api/test`)

点「测试连接」会**就地**用表单里的临时配置构造一个不入 STATE 的 `CloudLLM`,
发一句"回复两个字:可用"做连通性+JSON 能力探测,返回:

```json
{"ok": true, "reply": "可用", "json_ok": true, "latency_ms": 850}
```

失败时返回详细错误:`HTTP 401: Authentication Fails`、`HTTP 404: 模型不存在` 等,
前端按 401/404/超时分类给出可操作建议。**测试不消耗额度**(max_tokens=32),
可放心反复点。**保存前必测**,避免存了才发现 key 错。

### 首启引导
页面加载后若 `GET /api/settings.has_user_config = false`,会弹一次引导卡,
列出 3 步 + "稍后配置,先离线阅读" 跳过;`localStorage['lens.guided']` 防重复。
**离线(未启动服务)也会弹**,因为这时用户最需要知道"要先启动服务"。

**必须注意的遮挡问题**:引导卡是**居中弹层**,和 `.modal` 的层级/定位一样。
用户从右上角点「我的 API 设置」时,引导卡会**压在面板上面**,把「本机服务地址」
「API Key」这些关键字段全挡住,看起来像是"面板是空的/坏的"。
因此凡是打开居中弹窗的函数,开头都要调 `hideGuideTemporarily()`:
```js
function hideGuideTemporarily(){
  var g = $('#guide');
  if (g && g.classList.contains('on')) g.classList.remove('on');
}
```
已挂: `openApiModal` / `openPdfModal` / `openAskModal`。
注意它**不写 `lens.guided`**——用户这次只是去配置,下次打开仍应看到引导。
`guideGo`("立即配置 API")按钮走的是 `closeGuide()` + `openApiModal()`,会写标记,属正常关闭。

### 双击 HTML 打开时的 API 地址(重要,踩过的坑)

页面有两种打开方式,请求地址必须区别对待:

| 打开方式 | `location.protocol` | `API_BASE` | 请求发往 |
|---|---|---|---|
| 双击 `outputs/literature_lens.html` | `file:` | `http://127.0.0.1:8877` | 本机服务(跨域,靠 CORS) |
| 通过 `serve.py` / `.bat` 打开 | `http:` | `''`(空=同源) | 同源相对路径 |

**为什么必须区分**:`file://` 下 `fetch('/api/settings')` 会被解析成
`file:///D:/api/settings` → 必然失败,浏览器报的错就是 **`Failed to fetch`**。
早期版本没做这层处理,用户双击 HTML 后点「保存并启用」只看到 `Failed to fetch`,
根本不知道是地址问题。

实现要点(`template.html` 顶部):

```js
function detectApiBase(){
  const saved = lsGet('lens.apiBase');           // 用户可自定义端口
  if (saved) return String(saved).replace(/\/+$/, '');
  if (location.protocol === 'file:') return 'http://127.0.0.1:8877';
  return '';                                     // 同源
}
var API_BASE = detectApiBase();
function apiUrl(path){ return API_BASE + path; }  // 所有 fetch 都走它
```

- **所有 10 处 `fetch('/api/...')` 都必须写成 `fetch(apiUrl('/api/...'))`**,漏一处该功能就只在 file:// 下失效。
- `localStorage` 在受限环境会抛 `SecurityError`,故封装 `lsGet/lsSet/lsDel` 三个带 try-catch 的helper,**不要在关键路径直接调 localStorage**。
- PDF 上传返回的 `j.upload.url` 是相对路径 `/uploads/xxx.pdf`,必须 `apiUrl()` 补全后再存,否则 file:// 下灯箱/iframe 白屏。
- `serve.py` 的 `_send()` 已带 `Access-Control-Allow-Origin: *`,`do_OPTIONS` 处理预检 —— **这两处不能删**,否则 file:// 下所有 POST 都会被浏览器拦掉。
- 「我的 API 设置」里有一个**本机服务地址**输入框,用户改端口后写入 `localStorage['lens.apiBase']`,不必改代码。

### 离线降级:配置可复制 / 可下载

若保存时确实连不上服务(服务没启动),**不再显示生硬的错误**,而是弹一个「手动保存配置」小窗:

- 把待保存内容转成完整的 `config.user.json` JSON(单 API 模式自动把预设的 base_url/model/protocol 补全)
- 提供「复制 JSON」与「下载 config.user.json」两个按钮
- 提示用户放到项目根目录后启动服务即可生效

这样即使用户还没启动服务,也能先把配置拿到手,而不是卡在报错上。

### 接口载荷(三模式,均写入 `config.user.json` 后热重载)

```bash
# 模式 1:预设 + Key(1 行)
curl -X POST /api/settings -d '{"preset":"deepseek","api_key":"sk-..."}'

# 模式 2:单 API(完整自定义)
curl -X POST /api/settings -d '{"single":{"base_url":"...","model":"...","api_key":"..."}}'

# 模式 3:双 API(原兼容)
curl -X POST /api/settings -d '{"translation":{...},"reasoning":{...}}'
```

> **本版本核心改造:模型无关(provider-agnostic)。**
> 换模型不改代码 —— `config.json` 填 4 个字段即可;支持 OpenAI 兼容 / Anthropic / Gemini / Ollama 四种协议,
> 并自动吸收各家参数差异(`max_tokens` ↔ `max_completion_tokens`、`temperature` 不支持、`response_format` 不支持等)。

## 何时用

- 用户要「翻译文献 / 读文献 / 批量翻译 PDF」
- 用户要「找出文献的创新点 / 分析创新性」
- 用户要「点击问题生成知识点 / 概念讲解 / 交互式阅读」
- 用户要「文献阅读器 / 对照阅读 / 思维导图笔记 / 文献透镜」
- 用户要「换个模型跑 / 用别的模型 / 换成 Kimi/通义/Claude/GPT」→ 直接看下面「换模型」一节

## 换模型（本版本重点）

### 最小配置：4 个字段

`config.json` 的 `providers` 数组，每项最少只要：

```json
{
  "id": "kimi",
  "base_url": "https://api.moonshot.cn/v1",
  "api_key": "sk-xxx",
  "model": "moonshot-v1-128k"
}
```

`protocol` 省略时按 `base_url` 自动识别，`enabled` / `roles` / `weight` 都有默认值。

### 优先用 CLI，不手改 JSON

```bash
PY="C:/Users/Lenovo/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"

$PY scripts/llm_models.py --presets          # 看内置预设的 base_url / model
$PY scripts/llm_models.py --add kimi         # 交互式新增（回车接受预设默认值）
$PY scripts/llm_models.py --probe            # 探测全部 provider 连通性 + JSON 能力
$PY scripts/llm_models.py --probe kimi       # 只探一个
$PY scripts/llm_models.py --use kimi         # 切换 active_provider
$PY scripts/llm_models.py --list             # 看当前配置与生效项
```

`--add` / `--use` 写的是 `config.user.json`（本地覆盖文件），**不动随包的 `config.json`**，
便于把带 key 的配置排除在分发之外。

### 三种切换方式

| 方式 | 命令 / 写法 | 生效范围 |
|---|---|---|
| 全局切换 | `--use kimi` 或改 `active_provider` | 之后所有 pipeline 跑批 |
| 单次指定 | `pipeline.py --provider kimi` | 本次运行 |
| 分角色指定 | `pipeline.py --tr-provider glm --az-provider deepseek` | 翻译用便宜的、分析用强的 |

单篇追加同样支持：`_add_one.py paper.pdf --provider kimi`。

### 支持的四类协议

| protocol | base_url 示例 | 说明 |
|---|---|---|
| `openai`（默认） | `https://api.deepseek.com/v1` | OpenAI / DeepSeek / Kimi / 通义 / 硅基流动 / OpenRouter / 豆包 / vLLM / LM Studio / Ollama 的 OpenAI 兼容端口 |
| `anthropic` | `https://api.anthropic.com/v1` | Claude 原生 Messages API（自动走 `x-api-key` + 顶层 `system`） |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta` | Google 原生 `generateContent` |
| `ollama_native` | `http://127.0.0.1:11434` | Ollama 原生 `/api/chat`（**仅当用户明确要求时才启用**） |

### 已内置的自动容错（换模型最容易踩的坑）

| 坑 | 适配层的处理 |
|---|---|
| **模型把数组包进对象**（最高频） | `coerce_list()`：要求「只输出 JSON 数组」，模型却给 `{"terms":[...]}` → 自动从 `terms/items/data/result/list/output/entries` 等键里取数组。不做这层容错会**整批数据静默归零**（真实事故：术语 30 → 0） |
| **模型把对象包进外壳** | `coerce_dict()`：`{"data":{...}}` 或单元素数组 `[{...}]` → 自动取内层对象 |
| 新模型废弃 `max_tokens`，要 `max_completion_tokens` | 400 报错识别后自动换参数名重试 |
| 推理模型拒绝 `temperature` | 自动剔除该字段重试 |
| 网关不支持 `response_format` | 自动去掉，退化为提示词约束 + 宽松 JSON 解析 |
| 不支持 `system` 角色 | Anthropic 走顶层 `system`；Gemini 合并进 user |
| 返回体格式各异 | `content` 字符串 / 多模态分块数组 / `choices[0].text` / `reasoning_content` / Anthropic `content[]` / Gemini `candidates[]` 统一提取 |
| HTTP 200 但 body 带 `error` | 识别并抛可读异常，不静默返回空 |
| `base_url` 写法五花八门 | 自动补 `/v1`、自动补 `/chat/completions`；末尾斜杠不产生双斜杠；直接给完整 endpoint 也认 |
| 429 / 5xx | 指数退避重试 |
| 单个 provider 挂掉 | `fallback.on_error_switch_provider` 可切备用 |

### 配置技巧

- **key 放环境变量**：`"api_key": "${DEEPSEEK_API_KEY}"`，避免仓库落明文。
- **多配置叠加**：`--config a.json` 或环境变量 `LITERATURE_LENS_CONFIG`，后者覆盖前者。
- **上下文与输出上限**：`"ctx"` / `"max_output"`，用于判断长文翻译是否会被截断。
- **额外请求体/头**：`"extra_body"` / `"extra_headers"`，如 OpenRouter 需要 `HTTP-Referer`。
- **角色偏好**：`"roles": ["translate", "analyze"]`，`pick_for("translate")` 会优先挑它。

### 换完必须重跑

换模型**不会**改变已生成的数据。要让产出反映新模型，必须重跑：

```bash
$PY scripts/pipeline.py --source all --limit 3             # 全库重跑
$PY _add_one.py "D:/path/paper.pdf" --provider kimi        # 单篇追加
$PY scripts/render.py --data data/lens_data.json --out "outputs/文献透镜.html"
```

### 换模型的可行性答案

- **能用**：任何 OpenAI 兼容接口（覆盖国内主流 + OpenAI + OpenRouter + 自建 vLLM）以及 Claude / Gemini 原生接口。
- **不能直接用**：非上述四类协议的自定义 API —— 需要在 `llm_client._build()` 加一个分支，约 15 行。
- **本地模型**：`ollama_native` 协议已实现且自检通过，但**默认不启用** —— 用户的既有要求是「用接入 AGENT 里的模型，不用本地模型」。除非用户明确改口，不要主动启用。

## 核心特性（区别于普通翻译）

| 能力 | 说明 |
|---|---|
| 结构化翻译 | 段落编号回填，严格对齐不丢段；公式/DOI/引用/单位走保护位原样保留 |
| 术语锚定 | 先全文抽术语表，翻译时强制统一，避免同一术语多种译法 |
| 创新点提炼 | 每条含【是什么 / 为何新 / 原文证据 / 页码 / 把握度】，找不到证据不编造 |
| 知识点体系 | 由创新点派生，点击即展开；支持按需调用模型生成深度讲解 |
| 中文献自适应 | 识别到中文原文时自动切换为单栏，不做无意义「中译中」 |
| 零重依赖 | 服务端只用标准库；网页单文件内联，双击即开，离线可读 |

## 阅读工作台能力（网页端）

| 模块 | 交互 | 快捷键 |
|---|---|---|
| 速览卡 | 读前一屏概览：一句话结论 + 研究问题/空白/方法/数据/局限 + 创新点快捷跳转 | 点工具栏「速览卡」 |
| 悬浮译文 | 鼠标悬停英文段落弹出中文译文卡。**锚定段落定位**：右对齐段落右缘、默认弹在段落下方（放不下弹上方），位置固定不随鼠标抖动、不盖光标处文字 | — |
| 术语高亮 | 开启后术语在正文带虚线底纹；术语表按正文出现段数排序 | 点「术语高亮」 |
| 术语筛选 | 点术语 → 仅保留命中段落 + 前后各一段上下文，其余淡化（hover 可临时恢复）；命中 ≤1 段时不做淡化，改为提示并保留全文 | 点术语 / 「清除筛选」 |
| 笔记大纲 | 把研究框架 / 创新点 / 知识点 / 结论自动铺成可折叠层级大纲 | 循环切换「笔记」按钮 |
| 思维导图 | 纯 SVG 手绘放射状导图，节点可点击跳转到对应创新点或知识点 | 同上（第二档） |
| 护眼模式 | 米色暖底主题，长时阅读友好 | 点「护眼模式」 |
| 字号 / 行距 | 双滑杆实时调节正文排版 | — |
| **原文分屏** | 把 PDF 逐页渲染成图分屏展示，**图表 / 公式 / 双栏排版不丢**（对标小绿鲸的原文对照模式）。工具栏「原文对照」开关 → 页内 ‹ › 翻页 → 「加宽」拉大 → 点页卡右上 ⤢ 进灯箱全屏（Esc / ←→ 翻页）→ 「同步」让原文跟随阅读位置滚动；用户手动翻原文时联动自动暂停 1.4s 防抢滚动条 | 点「原文对照」 |
| **划词翻译** | 原文分屏里**划选文字 → 浮出工具条 → 点「翻译」**（对标小绿鲸）。先用滑窗匹配回已译段落，命中就地显示译文（离线秒出）；匹配不到且 `serve.py` 在跑时走 `/api/translate` 在线翻（中英自动判向）；都不可用则提示启动服务。另有「复制」按钮 | 划选文字 → 「翻译」 |
| **右栏三标签** | 创新点 / AI 助手 / 翻译 三个 tab 切换;创新点保留原 renderRail,新增 AI 助手多轮对话与独立翻译面板 | 点右栏 tab |
| **AI 助手多轮对话** | 右栏第二个 tab,锚定当前文献的多轮对话;4 个快捷动作(快速总结/提取创新点/方法局限/关键术语);消息流式追加,自动滚到底;Token 估算显示;支持 Ctrl/Cmd+Enter 发送 | 点右栏「AI 助手」tab |
| **独立翻译面板** | 右栏第三个 tab,通用翻译 / 学科翻译切换;选中即可翻译(划词自动填入);输入框 + 翻译按钮;Ctrl/Cmd+Enter 触发;显示模型与字符数 | 点右栏「翻译」tab |
| 全屏阅读 | 隐藏左右栏,正文居中大字,进入沉浸态 | `F` |
| 收藏与筛选 | 星标收藏文献,按来源 / 收藏筛选 | 点列表右上星标 |

设置与收藏写入 `localStorage`,下次打开自动恢复。

### 文献列表(对齐参考图:原文标题 + 翻译标题 + 类别 + 附件 + 标签 + 操作)

`renderList()` 已重写为多行卡片:

| 列 | 来源字段 | 派生规则 |
|---|---|---|
| 原文标题 | `meta.title` | 兜底 `meta.filename` |
| 翻译标题 | `profile.title_zh` | 无则隐藏整行 |
| 来源 | `meta.source_key` | LiyuAgent / 农户用水库 / 联网检索 / 题录导入 |
| 类别 | `docCategory(d)` | `abstract_only` → 摘要级;有 `journal` → 期刊文章;`web` → 在线;否则 → 论文 |
| 附件 | `hasOrig(d)` | 有原页 → "PDF ✓";摘要级 → "无 PDF" |
| 标签 | `profile.keywords[:3]` | 超出显示 "+N" |
| 元数据 | `meta.year` / `meta.cited_by` / `profile.domain` | 被引高亮紫色 |
| 操作 | `openpdf` 按钮 | 点击直接打开 PDF 阅读器并选中该篇 |

搜索 hay 扩充 `profile.title_zh` 和 `meta.journal`,中文标题/期刊也能命中。

### 正文是阅读流，不是卡片流

正文段落（`.src`）**无底色、无边框**——纯文字流，鼠标悬停时才浮现浅青底提示可悬浮译文；
译文卡（`.tgt`，中英对照模式）保留浅底 + 青绿左条做层级区分。这样作者 / 机构 / 邮箱
这类**短元信息段**不会排成一堆零散的小卡片（用户截图反馈过这个问题）。

页码标记（`.pg`）**只在换页处出现**（与上一块的 `page` 不同才渲染），不再每段都标。
判断逻辑在 `blockHtml()` 里对比 `cur.bilingual[idx-1].page`，与分屏联动的 `data-page`
互不影响。回归断言：`_test_ui.js` C 组「页码只在换页处标记」「正文段落无卡片底色」。

## 原页图片（原文分屏的数据来源）

- 流水线处理时 `extractor.render_page_images()` 把每页渲染成 JPG（pymupdf，zoom 1.25 /
  quality 58，**实测约 110-130KB/页**），落到 `data/pages/<docId>/pN.jpg`，
  JSON 里只记相对路径（`pages_img`），**不进 JSON 正文**。
- `render.py` 渲染时打包成**双层图**（`_collect_page_images()`）：

  | 字段 | 内容 | 用在哪 | 体积 |
  |---|---|---|---|
  | `s` | 预览图（PIL 缩到宽 460px / q42）转 base64 **内联** | 分屏面板直接显示，秒开 | 约原图 1/3 |
  | `hi` | 高清原图**相对网页的路径**（`data/pages/.../pN.jpg`） | 灯箱全屏查看 | 0（读磁盘） |
  | `ar` | 真实宽高比 `w/h` | 页卡 `aspect-ratio` 提前占位 | 0 |

- **摘要级条目没有原文页**（没拿到 PDF），「原文对照」按钮自动禁用。
- 只渲染参与分析的前 N 页（`limits.max_pages`，默认 8），页内会标注。
- 存量数据补渲染：`python _add_pages.py`（不重调 LLM，只补图），跑完重渲染网页。
- 关闭/调参：`config.json` 加 `"pages_images": {"enabled": false}` 或调 `zoom` / `quality`。

### 词级文本层（划词翻译的数据来源）

图片上选不了字，所以处理 PDF 时 `extractor.extract_word_boxes()` 顺带把每页的
**单词 + 坐标**（相对页宽高的千分比，0-1000 整数）抽出来存进 `pages_txt`，
`render.py` 不做任何处理直接随 docs 进网页。模板在页图上盖一层透明文字
（`.tl`，同 PDF.js text layer 的思路），字色透明但可选：
划选 → `mouseup` 弹 `#selBar` → 「翻译」。

- 体积实测 **20-30KB/页**（一篇 8 页约 200KB），四份网页分别 +0.9 / +0.6 / +0.2 / +1.7MB。
- 坐标存千分比而不是像素，**与渲染 zoom 解耦**，以后改 zoom 不会错位。
- 扫描版 PDF 抽不到词 → 对应页没有 `.tl`，点击回退为直接开灯箱（交互不丢）。
- 划选翻译的匹配逻辑：选中文字 8 字滑窗 vs 每个已译段落 source 的命中率 ≥45% →
  直接用现成译文（弹卡标注「匹配自第 N 页 · 相似度 x%」）；否则 POST `api/translate`。
- 中文档划选命中段落但无译文 → 明确提示「该段为中文原文，未提供译文」，不装死。
- 关掉：`"pages_images": {"words": false}`（不影响页图）。

### 双层图是为什么（踩过的坑：分屏出现一条条白条）

第一版把高清原图整张 base64 内联，7 篇 × 8 页 ≈ **8.5MB**。浏览器解析基线 JPEG 是
边下载边解码的，几十张 MB 级图同时排队，首屏每张只露出顶部一条——用户看到的就是
「一条条白条」。两处改动根治：

1. **内联层换成轻量预览**（460px / q42）。实测体积：8.51→3.13MB、5.21→2.00MB、
   3.52→1.31MB，三份合计 17MB → 合并版 6.31MB（降 63%）。预览图缓存在
   `prev_pN.jpg`，重渲染时按 mtime 命中缓存，不重复解码。
2. **页卡先占位再揭图**：`aspect-ratio` 用真实宽高比撑开高度，配 `.ph` 呼吸条纹 +
   「第 N 页 · 加载中」文案；`img.onload` / `onerror` 后移除 `.ph` 揭开。
   没有占位时图片未解码高度塌成 0，同样表现为白条。

灯箱优先读 `hi`（磁盘高清，无 PIL 依赖时自动回退内联预览）；HTML 被单独拷走时
`img.onerror` 再回退 `s`，**不会白图**。

## PDF 抽取要有适配性

PDF 是「排版指令流」，不是文档。同一份代码在不同 PDF 上会得到完全不同的坐标语义。
改 `extractor.py` 前先读这一节——下面每一条都是 131 份真实 PDF 实测踩出来的。

实测体检（`_pdf_probe.py` 扫本机全部 131 份）：

| 情况 | 份数 | 处理 |
|---|---|---|
| 加密 | 0 | `needs_pass` → 先试空密码 |
| 打开失败 | 0 | 返回空 + `err`，不抛异常 |
| 含扫描页 | 2 | 标记 `scanned_pages`，前端给提示 |
| 含旋转页 | 1 | `rotation_matrix` 变换 + 对齐校验 |
| 双栏排版 | 45 | 中缝密度法切栏后排序 |
| 坐标异常 | 1 | NaN/越界/零尺寸过滤 |

### 旋转页：坐标语义不一致（最隐蔽的坑）

- 现象：页图正常，划词选中的却是另一处的字——整页文字层**错位**。
- 根因：`page.get_text("words")` 返回的是**未经旋转**的坐标系，而
  `page.get_pixmap()` 产出的是**旋转后**的图。直接归一化必然越界/错位。
  实测某份 IF27.7 论文第 10 页（rot=90、rect 792×612）：raw 坐标 **35/231 越界**，
  页面覆盖仅 42.1%。
- 解法：用 `page.rotation_matrix` 把词框变换到旋转后坐标系。**不是**
  `derotation_matrix`（实测越界 48/231，更差）。
- 必须加**对齐校验**：变换后越界词 >2% 就回退 raw 并标 `rot_unaligned`，
  宁可错位也不要赌——有的 PDF 的 `/Rotate` 本身就和真实排版不一致。

### 双栏：阅读顺序

- 现象：划选出来的句子是「左栏一句 + 右栏一句」拼接的错句。
- 别用「相邻词 x0 的最大间隙」判中缝：中文期刊的中缝只有 **2% 页宽**（实测 482–518/1000），
  而左栏内部行首缩进造成的间隙反而更大，间隙法必然误判。
- 改用 **x 覆盖密度网格**（`_column_gutters()`）：统计每个 x 刻度被多少词覆盖，
  覆盖数 < `len(words)*0.15` 的连续竖条（≥15 单位、限页面中部 10%–90%）就是中缝。
  实测切出 (482,518)，左栏末 idx=79 → 右栏首 idx=80，顺序正确。
- 递归最多切 3 栏；栏内分行、行内左右排序（`_sort_reading()`）。
- 首页通栏（标题/摘要横跨整页）是**假双栏**，密度法会自然不触发。

### 扫描页 / 加密 / 异常坐标

- 扫描页：词数 < 25 且页内有图片 → 记入 `scanned_pages`；该页无 `.tl`，
  前端显示「该页是扫描件，无文字层，无法划词」，点击回退开灯箱（交互不丢）。
- 加密：`doc.needs_pass` → 先 `authenticate("")`；解不开就返回空 + `err`，不崩。
- 坐标健壮：NaN / 越界 / 零尺寸词框全部裁掉；单页上限 `WORD_MAX_PER_PAGE=2500`
  截断并记 `truncated_pages`（个别 PDF 一页能吐出上万词，会把网页撑爆）。

### 适配状态字段

`extract_word_boxes(..., with_meta=True)` 返回 `(词表, meta)`，meta 落进 JSON 的
`pages_txt_meta`，前端用它决定提示文案：

| 字段 | 含义 |
|---|---|
| `scanned_pages` | 扫描页（无文字层） |
| `rotated_pages` | 旋转页（已做坐标变换） |
| `truncated_pages` | 超密页被截断 |
| `no_text_pages` | 完全没有文字 |
| `encrypted` | 空密码解开的加密 PDF |
| `err` | 打不开 / 非 PDF / 需要口令 |

存量数据补词层：`python _add_pages.py --force-words`（不重调 LLM，只补图 + 词层），
跑完重渲染网页。

## 提示词清单（想改/复用提示词看这里）

所有 LLM 提示词共 **12 条**（4 条系统角色 + 8 条调用提示词），散落在
`translator.py` / `analyzer.py` / `serve.py`。要一次性看全或导出复用：

```bash
PY="C:/Users/Lenovo/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
"$PY" D:/LiteratureLens/_export_prompts.py
# → 提示词清单.md（便于复制改）+ 提示词清单.docx（便于存档）
```

脚本会**先做源码一致性校验**（12 项）：比对源码里是否仍含各提示词的关键片段，
源码改了而文档没更新会直接报 FAIL——所以改完提示词重跑一次即可同步。

| 编号 | 提示词 | 位置 |
|---|---|---|
| S1–S4 | 翻译引擎 / 术语专家 / 审稿人分析师 / 科研导师（四个角色） | `translator.py`、`analyzer.py` |
| P1–P3 | 术语抽取 / 批量翻译 / 单段重试 | `translator.py` |
| P4 | 论文画像 + 创新点 + 知识点（最核心，7000 tokens） | `analyzer.py · PROFILE_PROMPT` |
| P5 | 知识点深度讲解（点击时按需） | `analyzer.py · Analyzer.deepen` |
| P6 | 摘要级防编造约束（动态插入 meta） | `analyzer.py · build_meta` |
| P7–P8 | 划词翻译 中→英 / 英→中 | `serve.py · /api/translate` |

改提示词的硬约束：
- P2/P3 里的占位符清单必须和 `translator.PROTECT` 的 tag 对得上（加了新保护类型要同步）
- P4 最贵，改它等于改整体分析质量，先在小样本上试
- 提示词只保证「说清楚」，**不保证模型守约**——结构化容错在
  `llm_client.coerce_list / coerce_dict`，不要删

## 关键约束

- **`--out` 一律用 ASCII 文件名**。中文文件名会让预览服务的 URL 解码失败 → 用户「看不到」网页。
  渲染打印「已生成」不代表预览能打开，**交付前必须用 `os.listdir(outputs)` 确认文件在磁盘上**。
- **模型只用配置里启用的 provider**，默认是云端 API（DeepSeek / 智谱 GLM）。
  **不要主动启用本地 Ollama** —— 这是用户的既有要求。
- 翻译与分析**必须基于原文**；模型答不出的写「原文未提及」，不臆造。
- 改网页只改 `assets/template.html`，**改完必须重跑 `render.py`** 才会反映到产出网页；
  `outputs/文献透镜.html` 是渲染产物，直接改它会在下次渲染时被覆盖。

## 证据页与正文采样（已调优，改前必读）

创新点的 `evidence` / `page` 质量取决于 `analyzer.pick_body_blocks()` 怎么喂正文。早期版本存在
**「所有创新点页码都是 p1」** 的问题，根因与对策：

| 坑 | 根因 | 对策（已实现） |
|---|---|---|
| 证据全来自摘要，页码一律 p1 | 顺着正文截断 14000 字，全被摘要+引言吃掉 | 按章节分区采样：intro 32% / method 24% / result 24% / concl 20% |
| 模型不知段落真实页码 | 喂给模型的文本没有页码信息 | 每段前缀 `(pN)` 标注 + prompt 明确要求 page 取标注里的 N |
| BMC 等期刊摘要无独立标题行 | 摘要正文直接跟在刊名/通讯作者后，落进"前置"段 | `exclude_abstract=True` 剔除**所有 p1 段落**（pipeline 默认开启） |
| 分区没吃饱导致浪费配额 | 剔除摘要后 intro 桶空了 | 配额回填：总用量 < 80% 时按 method→result→concl→other→intro 补齐 |

效果对比（同一篇 17 页论文）：页码从 `{p1×5, p2×2, p3×2, p6×4, p14×3}` 变为 `{p3×2, p6×8, p7×3, p14×6}`，
**p1 归零**，证据全部来自方法/结果/结论章。

判断依据：跑完看全库页码概览，若某篇 insight 页码清一色 `1`，就该检查采样。

## 元数据抽取（journal / authors / doi）

`enrich_from_pdf_firstpage(pages, doc, pdfmeta=...)` 补全以下字段，优先级从高到低：

1. CSV 元数据（`农户用水行为_文献库/文献总清单.csv`，最权威）
2. PDF 内嵌元数据（`doc.metadata` 的 subject/journal/title）
3. 首页文本正则：
   - **期刊**：`KNOWN_VENUE_RX` 匹配 BMC / Nature / Journal of / Agricultural Water Management 等，
     且必须跳过 `PUB_NOISE_RX`（Creative Commons 许可证、版权、通讯作者、邮箱、DOI 链接等噪音行）
   - **DOI**：`DOI_RX` 抓 `10.xxxx/...`
   - **作者**：`EMAIL_RX` 定位通讯作者邮箱，取其上方 2 行内的姓名行

注意：BMC 系列首页前 10 行常被 Creative Commons 许可证整段占位，正则必须先用 `PUB_NOISE_RX` 过滤，
否则会把许可证文本误认成期刊名。

### 元数据陷阱：期刊名被抽成论文标题

`KNOWN_VENUE_RX` 里**绝不能**放 `Water` / `Land` / `Agriculture` / `Plants` / `Science` / `Cell`
之类的单个泛化词 —— 它们会匹配到论文标题本身。

真实事故：标题 `Water scarcity: A global hindrance to sustainable` 被 `Water` 分支命中，
期刊字段被填成了论文标题。

对策（已实现）：
1. 期刊正则只保留**足够具体**的模式（`Journal of X` / `BMC X` / `Cambridge Prisms: X` / `Water Resources Research`…）
2. `looks_like_title()` 反向排除：
   - 冒号/破折号**后超过 3 个词** → 是标题（期刊子刊名通常 1-3 词，如 `Cambridge Prisms: Water`）
   - `A/An/The ... of/for/to/in ...` 句式 → 是标题
   - 长度 >45 且含 `analysis / review / dynamics / impact / hindrance` 等 → 是标题

### 元数据陷阱：单篇追加不走 CSV

`_add_one.py`（单篇追加）必须和 `discover_farmers()` 一样读 `文献总清单.csv`。
早期版本只靠文件名猜测 + 首页正则，导致：
- `year` 抽成 2023（CSV 是 2025，PDF 内嵌元数据串了）
- `authors` 为空
- `journal` 抽成论文标题

现在 `_add_one.py` 的优先级：**CSV / 数据源发现结果 > 文件名年份兜底 > 首页正则**。

### 元数据陷阱：Elsevier 的 subject 字段（卷期页码 + DOI 混在一起）

ScienceDirect 系 PDF 的 `doc.metadata["subject"]` 是**一整句话**，不是期刊名：

```
Engineering Structures, 197 (2019) 109380. doi:10.1016/j.engstruct.2019.109380
```

`_journal_from_pdf_meta()` 早期直接把它当期刊名返回 → 期刊字段变成上述整串
（真实事故：`钢-UHPC` 那篇，期刊字段 = 刊名 + 卷期 + 页码 + DOI）。对策（已实现）：

- **`_clean_venue()`**：先按 `doi:` 切开，再去掉 `, 197 (2019) 109380` 这类卷期页码尾巴
  → `Engineering Structures`
- **DOI 兜底**：Elsevier 正文页通常**不印 DOI**，只存在于元数据里；
  抓不到就改成从 `pdfmeta` 的 `subject / title / keywords / identifier` 里搜
- **作者兜底**：Elsevier 首页不印通讯作者邮箱，首页正则必然落空 →
  退到 `pdfmeta["author"]`（如 `Kangkang Wang`）

### 元数据陷阱：文件名里的哈希串被当成标题

微信 / 网盘导出的文件名常带长哈希前缀：

```
371ba335cc934d955f41202b2d8cd14c_8fafa5479d221eb89f462639c7a4bc1b_8钢-UHPC(1).pdf
```

`guess_title_from_filename()` 会把这串哈希当标题写进 `doc["title"]`，而
`enrich_from_pdf_firstpage()` 原本只在 **title 为空** 时才补 → **标题永远是哈希串**。
对策（已实现）：

- `_looks_like_filename_title()` 反向识别垃圾标题：等于文件名主干 / 含 ≥16 位十六进制串 /
  含文件名里 ≥20 字符的片段
- 命中就覆盖：优先 `pdfmeta["title"]`（Elsevier 里是最完整标题），退而求其次才用首页最长行

> 注意：语言判定用的是 **文件名**（`_is_chinese`），阈值 25%。像 `8钢-UHPC(1).pdf`
> 这种「中文文件名 + 英文正文」的，中文占比只有 1/12 → 判为 `en`，走正常中英对照，符合预期。

### 存量数据回填元信息（不重调 LLM）

`_fix_meta.py` 只重跑 `enrich_from_pdf_firstpage()`，秒级完成、不花额度：

```bash
$PY _fix_meta.py data/lens_data_uhpc.json             # 全量回填
$PY _fix_meta.py data/lens_data_uhpc.json --only e1074b62
```

改完 `sources.py` 的元信息逻辑后，历史数据不会自动更新 —— 跑一次 `_fix_meta.py` 再重渲染即可。

### id 稳定性：只用文件名

`_make_id()` **只用 `md5(filename)`**，不要用 `md5(filename + title)`。
标题会被 CSV 元数据补全/修正 → id 随之改变 → 同一篇文献重跑后变成**两条重复数据**。

若库里已出现同文件两条（旧 id + 新 id），用 `_dedup.py` 清理（保留后出现的那条）。

## 排除伪章节标题

`pick_body_blocks` 内置 `real_heading()` 过滤，避免这些被当成章节：
- 期刊名行（`BMC Plant Biology`、`Journal of xxx`）
- **首页的所有 heading**（刊名 / 论文标题 / `RESEARCH` 栏目名）
- 含许可证 / 版权 / 邮箱 / DOI 的行
- 泛化词（`research` / `article` / `review` / `open access`）

## 环境

- Python：`C:\Users\Lenovo\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
  （已含 pymupdf / pypdf / jieba）
- 工程目录：`D:\LiteratureLens`
- 输出目录：`D:\LiteratureLens\outputs`
- **本机没有 Bash coreutils**（`ls`/`cat`/`tail`/`dirname` 都不存在）→ 一律用「Write 落 .py 脚本 + python 执行」，
  不要用 `ls` / `cat` / `find` / `tail` 命令。

## 数据源

| key | 路径 | 说明 |
|---|---|---|
| `liyu_raw` | `D:/LiyuAgent/data/raw/鲤鱼科研` | 按子领域分目录（人工智能/机器学习/土木工程/多模态模块/分子动力学…） |
| `farmers_water` | `D:/农户用水行为_文献库` | 中文OA文献 / 英文OA文献，元数据以 `文献总清单.csv` 为准 |

### 联网数据源（不再局限于本地上传的 PDF）

**这套检索层已独立成通用 Skill：`academic-web-search`**（装在
`~/.workbuddy/skills/academic-web-search`，零依赖，任何 Agent 都能命令行调用或 import）。
想让别的 Agent 也能联网查文献，直接让它加载那个 Skill 即可；两边的
`scripts/retriever.py` **同源**，`_sync.py` 每次同步都会把函数体镜像过去（头部保留通用版）。
改检索逻辑时跑一次 `_sync.py` 就能两边一致。

实测结论（2026-09-21 本机实测，**改代码前必读**）：

| 源 | 状态 | 说明 |
|---|---|---|
| **OpenAlex** | ✅ 主力 | 官方免费 API，无需 key。含摘要、被引数、**OA 全文 PDF 直链** |
| **arXiv** | ✅ | 官方 Atom API，预印本全文可直接下载 |
| **Crossref** | ✅ | DOI 与元数据最权威，通常**没有摘要/全文** |
| Semantic Scholar | ⚠️ | 无 key 时频繁 429 → 退避重试，失败静默降级 |
| Europe PMC | ⚠️ | 偶发超时，可选 |
| **知网 CNKI** | ❌ | 三个入口全部返回「安全验证」页（2154B），强制反爬，**无公开 API** |
| **维普 CQVIP** | ❌ | HTTP 412 直接拦截 |

**知网 / 维普不能自动抓取——这是事实，不要承诺能抓。**
合规替代路径：**站内检索 → 批量导出题录 → 导入解析**。
CNKI 支持导出 RefWorks / EndNote / NoteExpress / BibTeX，解析器见 `scripts/retriever.py`
的 `parse_refworks` / `parse_bibtex` / `parse_endnote` / `parse_export`。

**但中文文献并不是抓不到。** OpenAlex / Crossref 收录了大量中文期刊
（知网收录的期刊大多注册了 DOI，会同步进 Crossref → OpenAlex）。实测
`--query "灌溉 用水 农户"` 直接返回《资源科学》《自然资源学报》的中文论文，
部分还能下到 OA 全文：

```
1 2018 PDF  2/3  资源科学       农户风险偏好、风险认知对节水灌溉技术采用意愿的影响
2 2018 PDF  2/3  资源科学       节水灌溉技术认知、采用强度与收入效应
5 2018 摘要 2/3  自然资源学报    外部性视角下的节水灌溉技术补偿标准核算
```

所以准确的说法是：**能自动检索中文文献，但不是从知网抓，而是走 OpenAlex/Crossref 的
中文收录**；只有那些没注册 DOI、知网独占的刊物才需要导出题录导入。

两种处理深度：

| depth | 触发条件 | 产出 |
|---|---|---|
| `fulltext` | 拿到了 OA 全文 PDF | 完整流水线：术语抽取 + 全文中英对照 + 创新点 + 知识点 |
| `abstract` | 只有摘要（未开放获取） | 摘要中英对照 + **摘要级**创新点，`meta.abstract_only=True` |

摘要级条目会在 prompt 里强制约束「凡摘要未提及一律写『原文未提及』，不得编造」，
**不要把摘要级结果包装成全文级结论**。网页端与 JSON 都能通过 `depth` 字段区分。

## 用法

### 0. 换模型 / 自检

```bash
PY="C:/Users/Lenovo/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"

$PY scripts/llm_models.py --list        # 看配置
$PY scripts/llm_models.py --probe       # 探测连通性 + JSON 能力
$PY _selftest.py                        # 模型无关性自检（27 项，不消耗 API 额度）
$PY _verify_fix.py                      # 术语抽取 / 期刊正则 / CSV 元数据 回归（26 项）
$PY _selftest_retrieve.py               # 检索层自检（12 项，6 项离线 + 6 项联网）
$PY _verify_web.py data/lens_data_web.json   # 联网产出校验（两条分支 + 证据回溯）
```

`_selftest.py` 用本地假服务模拟 OpenAI / Anthropic / Gemini / Ollama 与各类刁钻网关，
**不消耗真实 API 额度**，改完适配层必跑。目标：27/27 通过。

`_selftest_retrieve.py` 前 7 项离线（题录解析三格式 + 自动识别 + DOI/摘要还原 +
参数分发 + 相关性闸门），后 5 项联网（OpenAlex / arXiv / 多源去重 / 假 PDF 拒绝 /
OA 下载 `%PDF` 头校验）。目标 12/12。

`_verify_web.py` 校验联网产出的两条分支：全文条目术语与证据页正常，
摘要级条目带 `abstract_only`、页码为「摘要」、**创新点证据能回溯到摘要原文**
（防编造的真实检验，不要拿「置信度高不高」当指标——摘要写得扎实时置信度本就该高）。

### 1. 批量处理（主入口）

```bash
cd D:/LiteratureLens

# 两个库各取 3 篇
$PY scripts/pipeline.py --source all --limit 3

# 只取 LiyuAgent raw
$PY scripts/pipeline.py --source liyu_raw --limit 3

# 只取农户用水库的中文文献
$PY scripts/pipeline.py --source farmers --limit 5 --lang 中文

# 翻译用便宜的模型、分析用强的模型
$PY scripts/pipeline.py --source all --limit 3 --tr-provider glm --az-provider deepseek
```

参数：
- `--source` `all|liyu_raw|farmers|web|import`
- `--limit` 每个来源取几篇（联网检索时＝**每个源**取几条）
- `--lang` `中文|英文`（仅 farmers 有效）
- `--max-pages` 每篇最多解析几页（默认取 config 的 `limits.max_pages`）
- `--tag` 输出数据文件后缀
- `--provider` / `--tr-provider` / `--az-provider` 指定模型
- `--config` 额外配置文件

### 1b. 联网检索 / 题录导入

**先看清单再处理**（推荐，避免浪费额度在不相关的文献上）：

```bash
# 检索预览
$PY scripts/lens_search.py "farmer irrigation water use behavior" \
    --sources openalex,arxiv,crossref --n 5 --download

# 只看开放获取、只看 2020 年后、按被引排序
$PY scripts/lens_search.py " irrigation water pricing" --oa-only \
    --year-from 2020 --sort cited

# 导入知网/维普导出的题录，先看解析对不对
$PY scripts/lens_search.py --import-file "D:/cnki_refworks.txt"
```

`lens_search.py` 末尾会直接打印下一步该跑的 `pipeline.py` 命令，照抄即可。

**处理：**

```bash
# 联网检索 + 处理（拿到全文走完整流程，只有摘要走摘要级）
$PY scripts/pipeline.py --source web \
    --query "farmer irrigation water use behavior" \
    --web-sources openalex,arxiv --limit 3 --oa-only --tag web

# 只要开放获取且有全文的（丢弃纯摘要条目）
$PY scripts/pipeline.py --source web --query "water scarcity agriculture" \
    --limit 5 --oa-only --fulltext-only

# 导入知网/维普题录（无 PDF，一律摘要级）
$PY scripts/pipeline.py --source import --import-file "D:/cnki_refworks.txt" --tag cnki
```

联网专用参数：
- `--query` 检索式（给了就走 web 源，可省略 `--source web`）
- `--web-sources` 逗号分隔：`openalex,arxiv,crossref,s2`
- `--oa-only` 只检索开放获取（**能显著提高拿到全文的比例**）
- `--year-from` / `--year-to` 年份区间
- `--sort` `relevance|cited|date`
- `--fulltext-only` 丢弃只有摘要的条目
- `--import-file` / `--import-format` 导入导出题录（`refworks|bibtex|endnote`，不给则自动识别）

下载策略（已实现，不要绕过）：
- OA 全文落到 `D:/LiteratureLens/downloads/`
- **必须校验文件头为 `%PDF-`**——大量所谓「PDF 直链」实际返回出版商落地页/验证码页，
  不校验会把 HTML 喂给抽取器然后报一堆莫名其妙的错
- 下载失败不报错，自动降级为摘要级条目
- **已下载的同名文件会复用**，重跑流水线不会重复下载

#### 联网侧已修的三个坑（改代码前必读）

1. **相关性闸门**：各源的 relevance 排序对英文长查询不可靠。
   实测 `farmer irrigation water use behavior` 在 arXiv 上命中了
   《Water Bridging Dynamics of Polymerase Chain Reaction》——只因含 "water"。
   `filter_relevant()` 按「标题+摘要命中几个检索关键词」过滤，默认阈值 2 且
   **按检索式词数收敛**（两词检索式只要求命中 1 个，避免误杀）。
   中文词多为 2 字，长度阈值对中英文分别处理，否则中文检索式会被整个清空。
2. **摘要级页码标成 p1**：摘要没有页码，但分析器会顺着提示词填 1，
   UI 上显示成 `p1` 会让人误以为引自全文。已统一改成「摘要」。
3. **术语只从开头抽**：原来取前 10 段做术语抽样，术语几乎全来自引言，
   方法段/结果段的核心术语被漏掉。改为**头+中+尾**取样，实测同一篇论文 9 → 18 个。

### 2. 渲染网页

```bash
$PY scripts/render.py --data data/lens_data.json --out "D:/LiteratureLens/outputs/literature_lens.html"
```

### 2b. 追加单篇（用户直接指定某个 PDF 时）

`pipeline.py` 面向「按数据源批量发现」，用户点名一篇 PDF 时改用追加脚本：

```bash
$PY _add_one.py "D:/农户用水行为_文献库/英文OA文献/xxx.pdf"
$PY _add_one.py "D:/path/paper.pdf" --provider kimi
```

它复用 `pipeline.process_one()` 走同一套流程，处理完**替换/追加**进现有 `lens_data.json`
（按 id 去重，不会覆盖已有文献），并打印下一步该跑的 render 命令。

### 2c. 合并多批产出（把几个网页合成一个）

跑过本地库 + 联网检索 + 中文联网后，`data/` 下会有多份 `lens_data*.json`，
对应多个散落的网页。合并成一个：

```bash
$PY _merge_data.py                      # 默认合并 lens_data / _net / _cn 三份
$PY _merge_data.py data/a.json data/b.json --out data/lens_data_all.json
$PY scripts/render.py --data data/lens_data_all.json --out "D:/LiteratureLens/outputs/文献透镜_全部.html"
```

规则：

- **按 doc id 去重，后出现的批次优先**（同一篇后来重跑过就用新版）。
- `merged_from` 记录每份来源的文件名/检索式/篇数，`stats` 重新汇总
  （篇数、全文数、摘要级数、创新点数、知识点数），不会沿用某一批的旧统计。
- 合并的是 JSON，**不重调 LLM**，秒级完成；合并后重渲染即可。
- 合并版体积 = 各批预览图之和（不是高清原图），17 篇 ≈ 6.3MB，仍可双击直接打开。

### 3. 启动服务(可选,启用「生成深度讲解 / AI 对话 / 在线翻译 / API 配置」)

```bash
$PY scripts/serve.py --data data/lens_data.json --port 8877 --open
# 或双击 启动文献透镜.bat
```

**`启动文献透镜.bat` 的行为(改前必读)**:
1. 按顺序探测 Python:`.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe`
   → `versions/3.13.12/python.exe` → `C:\Python314\python.exe` → PATH 里的 `python`。
   找到第一个存在的就用,避免写死路径失效后回落到系统 Python(可能缺依赖)。
2. **先检测 8877 是否已在监听**。已监听 → 直接 `start` 打开浏览器后退出,
   不重复启第二个服务(重复启动会因端口占用直接崩,用户会以为坏了)。
3. 渲染 → 起服务 → `--open` 自动开浏览器。关窗口即停。
4. 渲染失败不中止(继续用上一次生成的 HTML 启动),避免改模板出错时连旧版都看不了。

> 双击 HTML 也能读译文/创新点/知识点,但**上传 PDF、API 配置、AI 对话会全部失败**
> (file:// 协议下相对路径请求解析成 `file:///D:/api/...`)。要让这些功能可用,
> **必须先启动本机服务,再用 `http://127.0.0.1:8877/` 打开**。

服务额外提供:
- `GET /api/health` → 当前模型、provider 列表、文献数
- `GET /api/models` → 可用模型清单
- `GET /api/settings` → 可展示配置 + 11 个 API_PRESETS(供前端回填) + active_summary + has_user_config
- `POST /api/settings` → 保存用户 API(三模式:preset+key / single / dual),**热重载**无需重启
- `POST /api/test {preset|single|dual + api_key}` → 临时构造不入 STATE 的 CloudLLM,做连通性+JSON 探测,**不落盘**
- `POST /api/use {"provider":"kimi"}` → 运行时切换模型(不落盘)
- `POST /api/deepen` → 生成知识点深度讲解
- `POST /api/translate {"text":"...","mode":"general|academic"}` → 划词翻译的在线兜底(中英自动判向,学科模式保留术语规范译名)
- `POST /api/upload-pdf` → 校验并本机保存用户上传的 PDF,返回浏览器原生查看地址(最大值见 `uploads.max_mb`)
- `POST /api/mindmap {"doc_id":"...","question":"..."}` → 按当前文献生成带原文依据的问题思维导图,走 `mindmap` 角色的用户 API
- `POST /api/chat {"doc_id":"...","messages":[{"role","content"}]}` → 右栏 AI 助手的**多轮对话**接口,锚定当前文献+截断上下文,返回带模型名与字符估算

不启动服务时网页仍可完整阅读(译文 + 创新点 + 知识点),只是上传 PDF、在线翻译、深度讲解、AI 对话、API 配置与问题导图不可用。

## 工作流（Agent 视角）

1. 确认用户要处理的来源与数量；用户问「换模型」→ 先 `llm_models.py --list` 看现状
2. 跑 `pipeline.py` 生成 `data/lens_data.json`
3. 跑 `render.py` 生成单文件网页
4. **用 present_files 把网页交给用户**
5. 如用户要交互式深度讲解，再起 `serve.py`

## 产出结构

```
data/lens_data.json      —— 全量结构化数据（译文/创新点/知识点）
outputs/literature_lens.html —— 交互式单文件网页（主交付物，ASCII 文件名）
```

顶层字段带模型信息：`model`（分析模型）、`models.{translate,analyze}`、`provider.{translate,analyze}`、
`stats.by_provider`（按 provider 分组的调用数）。

每篇文献的数据字段：
- `meta`：标题/作者/年份/期刊/DOI/语言/来源
- `bilingual`：中英对照块数组，逐块带 `page` / `kind`(title|heading|para|formula)
- `terms`：术语对照表，`{en, zh}`
- `profile`：研究问题 / 空白 / 方法 / 数据 / 结论 / 局限 / 关键词
- `insights`：创新点数组，含 `what` / `why_new` / `evidence` / `page` / `confidence` / `concept_ids`
- `concepts`：知识点数组，含 `one_liner` / `explain` / `how` / `formula` / `pitfall` / `related` / `detail.deep`

## 改版验证（改模板或适配层后必做）

```bash
PY="C:/Users/Lenovo/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"

# 0. 改过 llm_client / config_loader → 跑自检（不花额度）
$PY _selftest.py

# 1. 重渲染
$PY scripts/render.py --data data/lens_data.json --out "D:/LiteratureLens/outputs/文献透镜.html"

# 2. 用 jsdom 跑真实交互测试（零依赖，走本机已装的 jsdom）
NODE_PATH="C:/Users/Lenovo/.workbuddy-ai/binaries/node/workspace/node_modules" \
  "C:/Users/Lenovo/.workbuddy-ai/binaries/node/versions/22.22.2-2/node.exe" \
  D:/LiteratureLens/_test_ui.js [网页路径]          # 50 项主交互，默认读 outputs/文献透镜.html

# 2b. 联网条目专用：摘要级徽标 / 被引 / 原文链接 / 页数不误显示
NODE_PATH="C:/Users/Lenovo/.workbuddy-ai/binaries/node/workspace/node_modules" \
  "C:/Users/Lenovo/.workbuddy-ai/binaries/node/versions/22.22.2-2/node.exe" \
  D:/LiteratureLens/_test_web_ui.js D:/LiteratureLens/outputs/文献透镜_联网检索.html

# 2c. 原文分屏 + 划词翻译：开关 / 翻页 / 加宽 / 文本层 / 工具条 / 本地匹配 / 灯箱
NODE_PATH="C:/Users/Lenovo/.workbuddy-ai/binaries/node/workspace/node_modules" \
  "C:/Users/Lenovo/.workbuddy-ai/binaries/node/versions/22.22.2-2/node.exe" \
  D:/LiteratureLens/_test_pages_ui.js [网页路径]

# 2d. 划词翻译在线接口（起服务线程实测，会产生一次真实 LLM 调用）
"C:/Users/Lenovo/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe" \
  D:/LiteratureLens/_test_translate_api.py

# 2e. 改过 PDF 抽取 → 跑适配自检（12 项：旋转坐标/双栏顺序/框合法性/兜底/截断）
"C:/Users/Lenovo/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe" \
  D:/LiteratureLens/_test_pdf_adapt.py

# 3. 真实浏览器端到端（jsdom 的升级版，必做——能抓到 jsdom 抓不到的跨域/协议问题）
#    前置：先起服务 `$PY scripts/serve.py --data data/lens_data.json --port 8877`
NODE_PATH="C:/Users/Lenovo/.workbuddy-ai/binaries/node/workspace/node_modules" \
  "C:/Users/Lenovo/.workbuddy-ai/binaries/node/versions/22.22.2-2/node.exe" \
  D:/LiteratureLens/_browser_e2e.js
```

**`_browser_e2e.js`（真实 Chrome，11 项）——为什么必须有它：**
jsdom 里 `location.protocol` 是 `about:`、也没有真实网络栈，所以
**`file://` 地址探测、CORS 预检、multipart 上传这三类问题 jsdom 一律测不出来**，
而这正是用户实际踩的坑。该脚本用 `playwright-core` 驱动**本机已装的 Chrome**
（可执行文件取 `C:\Users\Lenovo\.cache\puppeteer\chrome\win64-*\chrome-win64\chrome.exe`），
`chromium.launch()` 时带 `--allow-file-access-from-files`，覆盖：

| # | 检查项 |
|---|---|
| 1 | `file://` 打开无 JS 异常，`API_BASE` 自动探测为 `http://127.0.0.1:8877` |
| 2 | `file://` 页面跨域 `fetch(apiUrl('/api/health'))` 成功（验 CORS 真的通） |
| 3 | 「我的 API 设置」入口可点开 |
| 4 | `#apiBaseInput` 回显地址正确 |
| 5 | `POST /api/settings` 返回 200（**不再 Failed to fetch**） |
| 6 | `#pdfFile2` 上传 input 存在；`setInputFiles` 真实上传 PDF 后页面无「无法上传」提示 |
| 7 | 对照 `http://` 同源打开：`API_BASE` 应为空串、文献列表渲染正常、`/api/health` 同源可通 |

> **注意**：脚本会用假 key 调一次 `POST /api/settings`，从而写出 `config.user.json`。
> **跑完必须删掉它**，否则用户真实配置会被 `{"id":"user-api","api_key":"sk-test-..."}` 覆盖。

`_test_ui.js` 逐项验证：启动无 JS 错误、速览卡、悬浮译文、术语高亮与筛选（含弱术语不淡化）、
大纲与思维导图、护眼/字号/行距/全屏、收藏、文献切换、知识点抽屉。**目标：失败项 0、运行期错误 0。**

它会**自动挑一篇非中文源的文献**来测悬浮译文与术语表（中文原文走单栏 `zhonly`，不挂悬浮译文、
不渲染中英术语表，属预期行为）。若整个批次全是中文文献，这两项打印 `SKIP`，**不算失败**。

改模板时特别注意：
- 术语点击传的是 `encodeURIComponent` 后的值，`activeTerm` 存**解码后的原文**，比较前要统一。
- 术语筛选判定要**排除 heading 块**（标题天然含术语名，会造成假命中）。
- 术语筛选保留「命中段 + 前后各一段」，命中数 ≤1 时**不淡化**（藏掉几十段对阅读无意义）。

## 排查

| 现象 | 处理 |
|---|---|
| **术语数 0（术语高亮/筛选失效）** | 模型把数组包成了 `{"terms":[...]}`；`coerce_list()` 已修，若仍为 0 跑 `_verify_fix.py` 第 4 节诊断 |
| **期刊字段 = 论文标题** | `KNOWN_VENUE_RX` 混入了泛化单词；见「元数据陷阱：期刊名被抽成论文标题」 |
| **年份/作者错或空** | 单篇追加没走 CSV；`_add_one.py` 已修，重跑即可 |
| **同一篇文献出现两条** | id 用了 `filename+title` 导致标题修正后 id 变化；`_make_id` 已改为只用 filename，用 `_dedup.py` 清理存量 |
| 换了模型但产出没变 | 换模型不重算历史数据，要重跑 `pipeline.py` / `_add_one.py` |
| **点保存/测试 API 报 `Failed to fetch`** | 双击 HTML 打开时相对路径 fetch 必然失败。已用 `apiUrl()` + `API_BASE` 自动指向 `127.0.0.1:8877`;若仍报错,确认服务已启动,并在「我的 API 设置」顶部检查**本机服务地址**是否与实际端口一致 |
| **PDF 拖入后整个页面被替换成 PDF** | window 上没拦 `dragover/drop`。检查模板底部那两行全局 preventDefault 是否还在 |
| **重复选同一个 PDF 没反应** | `input[type=file]` 的 `onchange` 里忘了 `e.target.value = ''` 清空 |
| **PDF 上传成功但列表点开是白屏** | 上传返回的是相对路径 `/uploads/xxx.pdf`,忘了用 `apiUrl()` 补全成完整地址 |
| **所有 API 功能全挂、报 `Failed to fetch`** | 先确认服务在跑：`netstat -ano \| findstr :8877` 有 LISTENING 才算。没跑就双击 `启动文献透镜.bat`。**双击 HTML 只能离线读，不能配 API / 传 PDF** |
| **启动 bat 一闪而过 / 说找不到 python** | bat 会依次探测 3 个 Python 路径。若全不存在会明确提示装 Python 3.10+；不要写死路径，改 bat 里那个 `for %%P in (...)` 列表 |
| **端口被上次的服务占着，起不来** | bat 已内置检测：8877 在监听时直接开浏览器不再起第二个。若想强制重启，先关掉旧的黑窗口或 `taskkill /F /PID <pid>` |
| **打开 API 设置/PDF 导入，看不到要填的字段** | 首启引导卡也是居中弹层，会盖住面板。已加 `hideGuideTemporarily()`：`openApiModal` / `openPdfModal` / `openAskModal` 开头都调它。**新增任何居中弹窗都要记得调**，且该函数**不写 `lens.guided`**（用户下次仍能看到引导） |
| `调用失败（provider=xxx）` | 先跑 `llm_models.py --probe xxx` 看是连通性问题还是 key 问题 |
| 返回空内容 | 适配层已兜底 `reasoning_content`；若仍空，可能是模型只输出思维链，换 provider 试 |
| 模型不支持 JSON | 适配层会自动退化；若结构仍不稳，改用推理更强的 provider 做 analyze 角色 |
| 长文翻译被截断 | 调小 `translate.max_chars_per_block`，或换 `ctx` 更大的模型 |
| 中文文献译文栏是空的 | 正常，中文原文走单栏显示 |
| 公式块没翻译 | 设计如此，保护位原样保留 |
| 某段显示「本段未返回译文」 | 该段单独重试也没成功，通常是超长段或纯图表引用 |
| 创新点页码全是 p1 | 正文采样被摘要吃满，检查 `pick_body_blocks(exclude_abstract=True)` 是否开启 |
| **标题是一串哈希 / 就是文件名** | 文件名带 md5 前缀被 `guess_title_from_filename` 当标题；`_looks_like_filename_title` 已修，存量跑 `_fix_meta.py` 回填 |
| **期刊字段是一整句话（含卷期页码和 DOI）** | Elsevier 的 `subject` 字段没切分；`_clean_venue()` 已修，存量跑 `_fix_meta.py` 回填 |
| **期刊名/作者/DOI 为空** | 该期刊首页无独立标题行；检查 `PUB_NOISE_RX` 是否覆盖了该刊的许可证版式 |
| 章节标题里出现刊名或论文标题 | `real_heading()` 需要补充该刊的版式特征（当前已覆盖首页全排除） |
| 点了术语但全文都变灰 | 该术语在正文里几乎不出现，新版已改为不淡化并给出提示 |
| 改了模板但网页没变化 | 忘了重跑 `render.py` |
| HTTP 429 | 自带退避重试；持续失败就 `--provider` 换一个 |
| 联网检索 0 结果 | 检索式太长/太窄；先用 `lens_search.py` 试短词，或去掉 `--oa-only` |
| 一条全文都下不到 | 不加大 `--oa-only` 反而降低命中，检查是否走了 Elsevier/Springer 等非 OA 源；`--oa-only` + `--web-sources openalex,arxiv` 命中率最高 |
| 下到的 PDF 抽取报错 | 先确认文件头是 `%PDF-`；`download_pdf` 已校验，若手工下载需自查 |
| 知网/维普抓不到 | **正常，抓不了**（安全验证页 / HTTP 412）。改用站内导出题录 + `--import-file` |
| 题录导入解析出 0 条 | 用 `--import-format` 显式指定格式；`parse_export` 的自动识别兜底取「解析结果最多」的解析器 |
| 摘要级条目结论太虚 | 设计如此；`abstract_only=True` 已在 prompt 层禁止编造，要看细节就换有全文的条目 |
| 网页体积太大（>15MB） | 页图内联是大头；调小 `pages_images.zoom/quality`，或 `"enabled": false` 关掉后重跑 `_add_pages.py` 不会删图、重渲染即无图。双层图预览 + 词层之后：单批 1.5-4MB，合并版 ~8MB |
| **渲染显示成功但 outputs 里找不到网页** | `--out` 用了**中文文件名**：预览服务按 URL 解析时路径解码失败，文件在预览侧不可见（真实事故：`钢-UHPC组合梁_文献透镜.html`）。`--out` 一律用 ASCII 名，如 `steel_uhpc_lens.html`。**注意：`renders` 打印的「已生成」不等于预览侧能打开**，交付前用 `os.listdir(outputs)` 确认文件确实在，并按 ASCII 名交付 |
| **网页打开是空白/看不到内容** | 先确认文件名是 ASCII（见上条）；再确认交付的是 `render.py` 的产物而不是手改过的 HTML |
| 「原文对照」按钮是灰的 | 该文献没有原页图：摘要级条目（无 PDF）属正常；全文条目则是渲染失败，看控制台 / 重跑 `_add_pages.py` |
| 原文页显示不全 | 只渲染参与分析的前 N 页（`limits.max_pages`，默认 8），页内有标注；要全本就调大 max_pages 重跑 |
| 原文分屏出现一条条白条 | 老版本内联了高清原图导致解码排队。确认用的是双层图渲染：页卡应带 `.ph` + `aspect-ratio`，`_test_pages_ui.js` 有这两条断言 |
| 划选后没弹工具条 | 该页没有文本层（扫描版 PDF 抽不到词，或数据是加词层之前处理的）→ 跑 `_add_pages.py` 补词层后重渲染 |
| 点「翻译」提示启动服务 | 本地段落没匹配上（选中内容在图表/参考文献里）且没开 `serve.py`；启动后同样操作就走在线翻译 |
| 划词翻译结果对不上段落 | 匹配阈值 45%；选中跨段/含公式时可能偏，重选完整句子通常更准 |
| **原文页文字层整页错位** | 旋转页：词坐标是未旋转系、页图是旋转系。确认用了 `rotation_matrix`；见「PDF 抽取要有适配性」 |
| **双栏文献划选出来是错句（左右栏串在一起）** | 阅读顺序没按栏排。确认走的是 `_column_gutters()` 密度法而不是最大间隙法；`_pdf_probe.py` 会报该 PDF 是否双栏 |
| **某页提示「扫描件，无文字层」** | 该 PDF 是图片型（扫 131 份里有 2 份）。要划词需先 OCR；页图与灯箱仍可正常使用 |
| **PDF 打不开 / 提示需要口令** | `needs_pass` 已试空密码；真加密的 PDF 要用户提供口令。非 PDF 文件会返回空 + `err`，不抛异常 |
| PDF 相关问题想一次性体检 | 跑 `_pdf_probe.py` 扫全部 PDF，输出加密/扫描/旋转/双栏/坐标异常统计 |
| 灯箱全屏图加载失败 | `hi` 是相对路径，网页被单独拷走会读不到——代码已 `onerror` 回退内联预览；若仍白图，检查 `data/pages/` 是否跟着一起拷 |
| 几份网页想合成一份 | `_merge_data.py` 合并 JSON（按 id 去重、后批优先）后重渲染，不重调 LLM |
| `ModuleNotFoundError: fitz` | 用上面指定的 venv python，不要用系统 python |
| 路径写错 / 找不到文件 | 本机无 coreutils，用 python 脚本列目录，别用 `ls`/`find` |
