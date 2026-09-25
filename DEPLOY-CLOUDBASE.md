# 文献透镜后端 · 公开部署指南（微信云开发 云托管）

本指南让「文献透镜」后端跑在公网，使**任意设备、任意网络**打开小程序都能用
（不再依赖你电脑的局域网）。核心结论先行：

> ⚠ **必须用「云托管 / CloudBase Run（容器）」，不要用「云函数」**。
> serve.py 依赖 `fitz`(PyMuPDF) 做 PDF 渲染，这是带原生编译的包，
> 云函数环境装不上；而容器里 `pip install PyMuPDF` 有官方 wheel，原样跑 serve.py 即可。

---

## 一、部署架构（关键认知）

小程序功能分两类，部署后行为：

| 类别 | 功能 | 依赖 | 部署后 |
|------|------|------|--------|
| A 类（AI） | 翻译 / 对话 / 导图 / 深度讲解 | 直连你填的 LLM Key | 一直都能用（手机端已直连） |
| B 类（数据） | 文献库 / 上传PDF / 原文页图 / 解析 | **必须**有后端 | 部署后所有设备通用 |

所以「所有设备可用」= 把 B 类后端公开部署。手机端不再需要 `config.js` 写局域网 IP，
改成云托管给的**公网 HTTPS 域名**即可。用户资料/笔记/统计/翻译缓存仍在各自手机本地。

### 关于「后端没配 AI 模型也能跑」
容器里 **不需要** 预设 LLM Key：serve.py 已支持「零 provider 启动」——
PDF 存储 / 原文页图 / 文献列表等 B 类接口照常工作；翻译/对话/导图等 A 类接口会
返回友好的「后端未配置 AI 模型，请在网页设置或小程序『我的』里配置」提示，不崩溃。
因此两种运营方式都支持：
- **共享 Key**：在云托管环境变量里填 `LENS_API_KEY`，所有用户共用一个分析 Key。
- **各端自理**：不填 `LENS_API_KEY`，每个用户在「我的 API 设置」里填自己的 Key（A 类直连）。

---

## 二、准备镜像（两种路径，任选）

### 方案 A：本地 Docker 构建（可选，需本机装好 Docker）
适合已装 Docker Desktop 的机器，先本地跑通再 push。
```bash
cd D:\LiteratureLens
docker build -t literature-lens:latest .
docker run -d --name ll -p 8877:80 \
  -e PORT=80 -e LENS_API_KEY=sk-xxxx -e LENS_API_BASE=https://api.deepseek.com -e LENS_MODEL=deepseek-chat \
  -v ll_data:/data literature-lens:latest
curl http://127.0.0.1:8877/api/health
```

### 方案 B：代码仓库在线构建（**免本地 Docker，推荐**）
本机装 Docker 在不少无头/受限环境会卡 UAC（你之前就遇到过）。CloudBase Run 支持
**直接从 Git 仓库在线构建镜像**，你只需把仓库推到 Git，剩下的构建在云端完成：

1. 把 `D:\LiteratureLens` 推到任意 Git 仓库（GitHub / 腾讯工蜂 / GitLab 均可）。
   仓库里已配好 `.gitignore` 与 `.dockerignore`，**密钥与运行时数据都不会进仓库/镜像**。
2. 云托管「新建服务」时选「**从代码仓库构建**」，填写：
   - 仓库地址 + 分支（如 `main`）
   - **构建目录 / 上下文**：仓库根目录（`.`）
   - **Dockerfile 路径**：`Dockerfile`（已在根目录）
3. 云托管会拉代码 → 云端 `docker build` → 部署，全程无需你本机有 Docker。

> 本地验证镜像能否跑（可选）：用方案 A；或直接走方案 B，由云端构建日志反馈成败。

---

## 三、推到镜像仓库（仅方案 A 需要）

### 方案 A1：微信云托管自带镜像库（最简单）
1. 开通「微信云开发」→ 进入「云托管」→「我的镜像」。
2. 按控制台提示 `docker login` 到云托管镜像库，把方案 A build 的镜像 `tag` 后 `push`。

### 方案 A2：腾讯云 TCR / 阿里 ACR / Docker Hub
推到你自己的容器镜像仓库，云托管「新建服务」时填该镜像地址。

---

## 四、在云托管创建服务

1. 云开发控制台 → **云托管** → 新建服务（例如 `literature-lens`）。
2. 部署方式二选一：
   - **从镜像部署**（方案 A）：填镜像地址。
   - **从代码仓库构建**（方案 B）：填仓库/分支/Dockerfile，见上文。
3. 端口填 `80`（与 Dockerfile `EXPOSE 80` 一致；serve.py 读 `$PORT`）。
4. **挂载文件系统**：新建一个「文件存储 / CFS」，挂载到容器路径 `/data`
   （文献库 `lens_data.json`、上传 PDF、页图都落这里，重启不丢）。
5. 环境变量（在云托管服务配置里填）：
   - `PORT=80`（云托管一般会自己注入，可不填）
   - `LENS_API_KEY=sk-xxxx`        # 共享后端分析用的 Key（可选；不填则 AI 解析需各端自配）
   - `LENS_API_BASE=https://api.deepseek.com`（按你用的模型填）
   - `LENS_MODEL=deepseek-chat`
6. 部署完成后，云托管会给一个**默认公网域名**，形如
   `https://literature-lens-xxxx.ap-shanghai.run.tcloudbase.com`（也可在「自定义域名」绑你自己的备案域名）。
   **记下这个域名 `YOUR_DOMAIN`**，下一步要用。

---

## 五、小程序侧切换（改一个值）

编辑 `文献透镜小程序-源码/literature-lens-mp/common/config.js`：

```js
// 生产环境：改成云托管给的公网 HTTPS 域名（去掉末尾斜杠）
export const BASE_URL = 'https://literature-lens-xxxx.ap-shanghai.run.tcloudbase.com'
```

然后**重编译**小程序（HBuilderX CLI `compile_ai.py`）→ 导入微信开发者工具。

### 微信公众平台加白名单（必须）
登录 [微信公众平台](https://mp.weixin.qq.com) → 开发管理 → 开发设置 → 服务器域名：
- **request 合法域名**：加 `YOUR_DOMAIN`
- **uploadFile 合法域名**：加 `YOUR_DOMAIN`（上传 PDF 用）
- **downloadFile 合法域名**：加 `YOUR_DOMAIN`（页图本地落盘前仍可加，保险）

> 真机调试时记得在开发者工具勾选「不校验合法域名…」可先跳过，但正式发布前必须配齐。

---

## 六、多人共用 vs 各用各的

- 云托管后端是**一套共享服务**：所有用户看到同一份文献库（谁上传的 PDF 大家都能搜到）。
- 手机端「我的 API 设置 / 研究方向 / 笔记 / 关键句 / 翻译缓存 / 统计」**仍只存在各自设备本地**，
  不会互相串。
- 若想「每个人独立的文献库」（多租户），需要额外加账号体系与按用户隔离，本期未做。

---

## 七、运维要点

- **持久化**：务必挂 `/data` 卷；不挂则容器重建后文献库与上传 PDF 全丢。
- **密钥**：`LENS_API_KEY` 走环境变量，不写进镜像；若用自定义域名 + 自己的 Key 管理更稳。
- **config.json 是本地配置，不会进镜像**：`Dockerfile` 只 `COPY scripts/` 与
  `docker-entrypoint.sh`，根目录的 `config.json`（`D:/...` Windows 路径、本地 sources 库）
  **不在镜像内**，也不会被加载——云端配置完全由 entrypoint 依据 `LENS_API_*` 环境变量生成。
  因此 Windows 绝对路径、本地 LiyuAgent 资料库路径都不会影响 Linux 容器。
- **扩容**：云托管按实例数/CPU 弹性，PDF 渲染吃 CPU，真多人用可调高实例规格。
- **日志**：云托管控制台看容器日志，关键词 `[entrypoint]` / `文献透镜 已启动`。
- **页图路径**：云端页图落在 `/data/pages`，已与 serve.py 的读取路径对齐（v 修复点）。

---

## 八、已知修复记录（仓库已含）

| 问题 | 修复 |
|------|------|
| `docker-entrypoint.sh` 在仓库根目录，旧 Dockerfile 只 `COPY scripts/` 导致入口脚本缺失、构建/启动失败 | Dockerfile 已追加 `COPY docker-entrypoint.sh /app/docker-entrypoint.sh` |
| 云端页图 404：写入 `/data/pages` 但读取走 `/app/data/pages` | `/data/pages/` 路由改为按 `data_path` 同级目录定位，与写入一致 |
| 未配 Key 时后端启动即 `SystemExit` 崩溃 | `require_provider=False` + LLM 接口友好拦截，B 类接口照常服务 |
| 启动横幅 `llm.provider()` 抛异常崩溃 | 横幅对空 provider 做了 try/except 兜底 |
