# 文献透镜 LiteratureLens

**把科研 PDF 变成一个可交互的精读工作台。**

上传一篇论文，得到：中英对照译文、术语表、创新点卡片、知识点深度讲解，
以及一个内置的原生 PDF 阅读器（划选翻译 / 术语高亮 / 笔记大纲 / 问题导图）。

支持小程序端与网页端，后端可跑在微信云托管，多用户数据严格隔离。

---

## 功能

| 模块 | 说明 |
|---|---|
| **中英对照翻译** | 按段落对齐，保留公式、引文与术语；长文自动分批并发，显著提速 |
| **术语表** | 自动抽取领域术语，可筛选、可高亮 |
| **创新点卡片** | 依据原文提炼贡献点，点击知识点标签滑出**深度讲解**（定义 / 原理 / 公式 / 易错点 / 关联概念） |
| **AI 问答** | 锚定当前文献按问题相关性检索片段后作答；原文无法支持时明确标注「原文未提及」，不编造 |
| **PDF 阅读器** | 原页图渲染 + 词级坐标，划选翻译、标记关键句、护眼/全屏 |
| **微信小程序** | 与网页端共用后端；按微信 openid 做多租户隔离 |

---

## 架构

```
┌──────────────┐      ┌─────────────────┐      ┌──────────────┐
│  小程序端     │      │  网页端          │      │  用户自备 LLM │
│ (uni-app)    │      │ (assets/模板)    │      │  OpenAI 兼容  │
└──────┬───────┘      └────────┬────────┘      └──────▲───────┘
       │  callContainer         │ HTTP                  │
       └───────────┬────────────┘                       │
                   ▼                                    │
        ┌──────────────────────┐                        │
        │  serve.py (stdlib)   │────────────────────────┘
        │  HTTP 服务 + 多租户    │
        ├──────────────────────┤
        │ pipeline / extractor │  PyMuPDF 渲染与抽取
        │ translator / analyzer│  翻译与创新点分析
        │ render               │  生成交互式网页
        └──────────┬───────────┘
                   ▼
        data/users/<openid>/    每人独立：文献库 / 上传 / API Key
```

**后端零框架依赖**：仅用 Python 标准库 `http.server`，第三方依赖只有 3 个（均带预编译 wheel）。

---

## 快速开始

### 1. 装依赖

```bash
pip install -r requirements.txt      # PyMuPDF / Pillow / pypdf
```

### 2. 配置 API

```bash
cp config.user.example.json config.user.json
# 编辑 config.user.json，填入你自己的 API（OpenAI 兼容 / Anthropic / Gemini / Ollama 均可）
```

> 推荐用环境变量而非明文密钥，例如 `"api_key": "${YOUR_API_KEY}"`。

### 3. 启动

```bash
python -m scripts.serve --port 8000
```

打开 `http://127.0.0.1:8000` 即可使用。

也可以作为命令行工具批量处理：

```bash
python -m scripts.pipeline --input ./papers --output ./out
```

---

## 部署到微信云托管

完整步骤见 **[DEPLOY-CLOUDBASE.md](DEPLOY-CLOUDBASE.md)**。

要点：

- 必须用**云托管（容器）**，不要用云函数 —— PyMuPDF 需原生环境
- 容器**无需预设 LLM Key**，各端用户在「我的」里自行填写
- 多用户隔离依赖微信网关注入的 `X-WX-OPENID`
- 建议挂载持久化存储并设实例数 = 1

---

## 隐私与安全

本项目的多租户隔离遵循一条铁律：**身份只能来自服务端可验证的来源。**

- 主通道走 `callContainer` 时使用微信网关注入的 `X-WX-OPENID`（不可伪造）
- 公网兜底通道使用 `wx.login` 的 code 由**后端**调 `jscode2session` 换取 openid（后端持 AppSecret 校验）
- 私有接口（文献库、配置、上传文件）拿不到 openid **一律返回 401**，绝不回退到任何共享库
- 每个用户的数据落在各自独立的 `data/users/<openid>/` 目录，互不可见

> ⚠️ 曾有一版误用「前端本地随机值」作为身份，导致同一设备换号后能看到前一个用户的
> 文献库与 API Key。该做法已被彻底移除，并加入回归测试防复发。

---

## 项目结构

```
scripts/
  serve.py         HTTP 服务入口 + 多租户隔离 + 上传/解析/轮询
  pipeline.py      端到端流程编排
  extractor.py     PDF 文本与坐标抽取（PyMuPDF）
  translator.py    分段翻译（并发批处理）
  analyzer.py      术语抽取 / 创新点 / 知识点讲解
  render.py        生成交互式阅读网页
  llm_client.py    LLM 客户端（多协议 / 重试 / 超时）
  config_loader.py 配置加载与校验
assets/
  template.html    网页端模板
```

---

## 许可

[Apache License 2.0](LICENSE)
