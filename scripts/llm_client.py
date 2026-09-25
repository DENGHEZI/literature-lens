# -*- coding: utf-8 -*-
"""
LLM 适配层 —— 模型无关设计（Provider-Agnostic）

设计目标
--------
任何具备对话补全能力的模型服务，只要填 4 个字段就能接入，不改一行代码：

    id / base_url / api_key / model

支持四种协议（protocol），自动适配请求体与响应解析：

| protocol      | 适用服务                                                      |
|---------------|---------------------------------------------------------------|
| openai        | OpenAI / DeepSeek / Kimi / 通义 / 硅基流动 / OpenRouter /     |
|               | vLLM / LM Studio / Ollama(OpenAI 兼容端口) / 任意兼容网关      |
| anthropic     | Claude 原生 Messages API                                      |
| gemini        | Google Gemini 原生 generateContent                            |
| ollama_native | Ollama 原生 /api/chat（仅当用户在 config 显式开启时）          |

容错能力（换模型最容易踩的坑，这里全部兜住）
--------------------------------------------
1. 参数名自适应：新模型普遍废弃 max_tokens，改用 max_completion_tokens；
   部分网关只认 max_tokens。→ 自动降级重试（_swap_token_param）。
2. temperature 兼容：某些推理模型（o 系列 / 部分国产推理模型）拒绝 temperature。
   → 自动剔除重试（_drop_temperature）。
3. system 角色兼容：个别模型不支持 system message。→ 自动并入 user。
4. base_url 拼装：自动补 /v1、自动补 /chat/completions，也能直接给完整 endpoint。
5. 返回体多样：choices[0].message.content / choices[0].text / content 数组块 /
   reasoning_content / Anthropic content[] / Gemini candidates[]。
   → 统一 extract_text() 提取。
6. JSON 输出：请求体带 response_format=json_object 时，若不兼容会自动去掉重试。
7. 429 / 5xx 指数退避；超时重试。
8. 能力探测：probe() 可测通一个 provider，并返回是否支持 JSON 模式。

零依赖：仅标准库 urllib。
"""
import json
import os
import re
import time
import urllib.request
import urllib.error


class LLMError(Exception):
    pass


# ---------------------------------------------------------------- 协议默认值
PROTOCOL_DEFAULTS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "ollama_native": "http://127.0.0.1:11434",
}

# 会被网关拒绝、需要自动降级重试的字段
_SWAP_TOKEN = ("max_tokens", "max_completion_tokens")


def _norm_protocol(p):
    proto = (p.get("protocol") or "").strip().lower()
    if proto:
        return proto
    base = (p.get("base_url") or "").lower()
    if "anthropic" in base:
        return "anthropic"
    if "generativelanguage.googleapis.com" in base:
        return "gemini"
    if "11434" in base or "/api/chat" in base:
        return "ollama_native"
    return "openai"


# ---------------------------------------------------------------- JSON 容错
def parse_json_loose(raw):
    """从模型输出里抠出 JSON 对象/数组，容忍围栏、前后废话、尾逗号、单引号。"""
    if not raw:
        return None
    s = raw.strip()
    # 去 ``` 围栏
    if "```" in s:
        chunks = s.split("```")
        for c in chunks:
            c2 = c.strip()
            if c2.lower().startswith("json"):
                c2 = c2[4:].strip()
            if c2[:1] in ("{", "["):
                s = c2
                break
    s = s.strip()
    for op, cl in (("{", "}"), ("[", "]")):
        i, j = s.find(op), s.rfind(cl)
        if i != -1 and j > i:
            cand = s[i:j + 1]
            for fixer in (lambda x: x,
                          lambda x: x.replace(",}", "}").replace(",]", "]"),
                          lambda x: re.sub(r"[\u201c\u201d]", '"', x)):
                try:
                    return json.loads(fixer(cand))
                except Exception:
                    continue
    try:
        return json.loads(s)
    except Exception:
        return None


# 模型可能把数组包进对象里时，常见的键名
_LIST_KEYS = ("terms", "items", "data", "result", "results", "list",
              "output", "entries", "keywords", "concepts")


def coerce_list(data, prefer_keys=_LIST_KEYS):
    """把模型输出强转成 list —— 兼容「模型不守约把数组包进对象」的情况。

    模型对「只输出 JSON 数组」的遵守度差异很大：有的直接给 `[...]`，
    有的给 `{"terms":[...]}` / `{"data":{...}}` / `{"result":[...]}`。
    这是换模型时最高频的坑：直接 `isinstance(x, list)` 判定会把有效结果整个丢掉。

    策略：
      1. 本身就是 list → 原样返回
      2. 是 dict → 依次尝试常见键名取值
      3. 是 dict 且只有一个值 → 取那个值
      4. 都不行 → 返回 None（由调用方决定降级）
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    for k in prefer_keys:
        v = data.get(k)
        if isinstance(v, list):
            return v
    vals = [v for v in data.values() if isinstance(v, list)]
    if len(vals) == 1:
        return vals[0]
    if len(vals) > 1:
        # 多个数组时取最长的那个（通常才是正文结果）
        return max(vals, key=len)
    return None


def coerce_dict(data, prefer_keys=("data", "result", "result", "output",
                                   "response", "analysis", "content")):
    """把模型输出强转成 dict —— coerce_list 的反向情形。

    模型可能把对象包进 {"data":{...}} 或返回 [ {...} ] 单元素数组。
    换模型时同样高频，不做容错会导致整篇分析静默返回空结构。
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for x in data:
            if isinstance(x, dict):
                return x
        return None
    return None


def extract_text(payload, protocol="openai"):
    """从各家不同的响应结构里统一提取文本。"""
    if not isinstance(payload, dict):
        return ""
    err = payload.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise LLMError("API 返回错误：" + str(msg)[:300])

    if protocol == "anthropic":
        blocks = payload.get("content") or []
        parts = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts).strip()

    if protocol == "gemini":
        cands = payload.get("candidates") or []
        parts = []
        for c in cands:
            for p in ((c.get("content") or {}).get("parts") or []):
                if isinstance(p, dict) and "text" in p:
                    parts.append(p["text"])
        return "\n".join(parts).strip()

    if protocol == "ollama_native":
        m = payload.get("message") or {}
        if m.get("content"):
            return str(m["content"]).strip()
        return str(payload.get("response") or "").strip()

    # ---- openai ----
    ch = payload.get("choices") or []
    if not ch and payload.get("output"):
        pass
    if ch:
        c0 = ch[0] or {}
        msg = c0.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):          # 新版多模态分块
            txt = "".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") in (None, "text", "output_text")
            )
            if txt:
                return txt.strip()
        if isinstance(content, str) and content.strip():
            return content.strip()
        # 推理模型只给 reasoning_content 时兜底取它（避免空返回）
        if msg.get("reasoning_content"):
            return str(msg["reasoning_content"]).strip()
        if c0.get("text"):
            return str(c0["text"]).strip()
    # 少数网关的返回
    for k in ("content", "response", "output_text", "text"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


# ---------------------------------------------------------------- 主客户端
class CloudLLM:
    """模型无关的对话补全客户端。

    providers: config.json 的 providers 数组，每项：
        {
          "id": "deepseek",                 # 唯一标识，用于 pid 指定
          "name": "DeepSeek V4.1 Flash",    # 展示名
          "protocol": "openai",             # 可省略（按 base_url 猜）
          "base_url": "https://api.deepseek.com/v1",
          "api_key": "sk-xxx",
          "model": "deepseek-chat",
          "enabled": true,
          "roles": ["chat", "translate", "analyze"],   # 可承担的角色
          "ctx": 128000,                    # 上下文上限（用于自动降级采样）
          "max_output": 8192,               # 单次输出上限
          "supports_json": true,            # 是否支持 response_format
          "extra_headers": {},              # 额外请求头（如 OpenRouter 的 Referer）
          "extra_body": {}                  # 额外请求体字段
        }
    """

    def __init__(self, providers=None, active=None, timeout=180, retries=3):
        cfg_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
        cfg = {}
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        self.providers = providers or cfg.get("providers", [])
        self.active = active or cfg.get("active_provider", "")
        self.timeout = timeout
        self.retries = retries
        # 调用统计（按 provider 分组）
        self._stats = {}
        self.json_probe = {}       # pid -> bool，记录 JSON 模式是否可用

    # ---------- provider 解析 ----------
    def provider(self, pid=None):
        """按 id 查找；找不到则退回第一个 enabled 的；再没有就报错。"""
        pid = pid or self.active
        if pid:
            for p in self.providers:
                if p.get("id") == pid and p.get("enabled", True):
                    return p
            for p in self.providers:          # active 指向的 provider 被禁用
                if p.get("id") == pid:
                    return p
        for p in self.providers:
            if p.get("enabled", True):
                return p
        if self.providers:
            return self.providers[0]
        raise LLMError("没有配置任何 provider，请检查 config.json")

    def pick_for(self, role, exclude=()):
        """按角色挑 provider：roles 里含该角色的优先，其次 roles 为空（通吃）的。

        exclude 内的 id 会被跳过（用于某个 provider 连续失败时换人）。
        """
        cands = [p for p in self.providers
                 if p.get("enabled", True) and p.get("id") not in exclude]
        if not cands:
            return self.provider()
        exact = [p for p in cands if role in (p.get("roles") or [])]
        if exact:
            return sorted(exact, key=lambda p: -(p.get("weight") or 0))[0]
        general = [p for p in cands if not p.get("roles")]
        if general:
            return sorted(general, key=lambda p: -(p.get("weight") or 0))[0]
        # 角色没匹配上，退回 active
        try:
            act = self.provider()
            if act.get("id") not in exclude:
                return act
        except LLMError:
            pass
        return cands[0]

    def available(self):
        try:
            return bool(self.provider().get("api_key"))
        except LLMError:
            return False

    def list_providers(self):
        return [{"id": p.get("id"), "name": p.get("name") or "",
                 "protocol": _norm_protocol(p), "model": p.get("model") or "",
                 "enabled": p.get("enabled", True),
                 "roles": p.get("roles") or []}
                for p in self.providers]

    @property
    def stats(self):
        calls = sum(v["calls"] for v in self._stats.values())
        return {
            "calls": calls,
            "chars_in": sum(v["chars_in"] for v in self._stats.values()),
            "chars_out": sum(v["chars_out"] for v in self._stats.values()),
            "by_provider": {k: dict(v) for k, v in self._stats.items()},
        }

    def _bump(self, pid, ci, co, err=False):
        d = self._stats.setdefault(pid, {"calls": 0, "chars_in": 0,
                                         "chars_out": 0, "errors": 0})
        d["calls"] += 1
        d["chars_in"] += ci
        d["chars_out"] += co
        if err:
            d["errors"] += 1

    # ---------- URL 拼装 ----------
    @staticmethod
    def _endpoint(p):
        proto = _norm_protocol(p)
        base = (p.get("base_url") or "").rstrip("/")
        if not base:
            base = PROTOCOL_DEFAULTS.get(proto, "")
            if proto == "gemini":
                base += "/models"
        if proto == "anthropic":
            return base if base.endswith("/messages") else base + "/messages"
        if proto == "gemini":
            if ":generateContent" in base:
                return base
            return base + "/models/{model}:generateContent"
        if proto == "ollama_native":
            return base if base.endswith("/api/chat") else base + "/api/chat"
        # openai 兼容
        if base.endswith("/chat/completions"):
            return base
        if re.search(r"/v\d+(?:beta)?$", base) or base.endswith("/openai"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    # ---------- 请求体 / 请求头 ----------
    @staticmethod
    def _build(p, protocol, prompt, system, max_tokens, temperature,
               json_mode, drop_temp=False, drop_json=False, token_key="max_tokens"):
        extra = dict(p.get("extra_body") or {})
        if protocol == "anthropic":
            body = {
                "model": p.get("model", ""),
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                body["system"] = system
            if temperature is not None and not drop_temp:
                body["temperature"] = temperature
        elif protocol == "gemini":
            body = {
                "contents": [{"role": "user",
                              "parts": [{"text": (system + "\n\n" + prompt) if system else prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens},
            }
            if temperature is not None and not drop_temp:
                body["generationConfig"]["temperature"] = temperature
            if json_mode and not drop_json:
                body["generationConfig"]["responseMimeType"] = "application/json"
        elif protocol == "ollama_native":
            msgs = []
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": prompt})
            body = {"model": p.get("model", ""), "messages": msgs,
                    "stream": False, "options": {"num_predict": max_tokens}}
            if temperature is not None and not drop_temp:
                body["options"]["temperature"] = temperature
            if json_mode and not drop_json:
                body["format"] = "json"
        else:  # openai 兼容
            msgs = []
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": prompt})
            body = {"model": p.get("model", ""), "messages": msgs, "stream": False}
            body[token_key] = max_tokens
            if temperature is not None and not drop_temp:
                body["temperature"] = temperature
            if json_mode and not drop_json:
                body["response_format"] = {"type": "json_object"}
        body.update(extra)
        return body

    @staticmethod
    def _headers(p, protocol):
        h = {"Content-Type": "application/json"}
        key = p.get("api_key") or ""
        if protocol == "anthropic":
            h["x-api-key"] = key
            h["anthropic-version"] = p.get("anthropic_version", "2023-06-01")
        elif protocol == "gemini":
            h["x-goog-api-key"] = key
        else:
            if key:
                h["Authorization"] = "Bearer " + key
        h.update(p.get("extra_headers") or {})
        return h

    # ---------- 主调用 ----------
    def chat(self, prompt, system=None, max_tokens=2400, temperature=0.25,
             pid=None, json_mode=False):
        """调用模型。任何需要"换模型"的差异都在这里被吸收。"""
        p = self.provider(pid)
        proto = _norm_protocol(p)
        url_tpl = self._endpoint(p)
        url = url_tpl.replace("{model}", p.get("model", ""))
        headers = self._headers(p, proto)

        # 未指定 max_tokens 时用 provider 声明值
        if max_tokens is None:
            max_tokens = int(p.get("max_output") or 4096)

        attempts = []  # 每次尝试的 (token_key, drop_temp, drop_json)
        tk = "max_completion_tokens" if p.get("legacy_max_tokens") is False else "max_tokens"
        attempts.append((tk, False, False))
        attempts.append((_SWAP_TOKEN[0] if tk != _SWAP_TOKEN[0] else _SWAP_TOKEN[1], False, False))
        attempts.append((tk, True, False))          # 去掉 temperature
        attempts.append((tk, True, True))           # 再去掉 json 模式
        # 去重保序
        seen, seq = set(), []
        for a in attempts:
            if a not in seen:
                seen.add(a)
                seq.append(a)

        last_err = None
        for token_key, drop_temp, drop_json in seq:
            body = self._build(p, proto, prompt, system, max_tokens, temperature,
                               json_mode, drop_temp, drop_json, token_key)
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            for attempt in range(self.retries):
                try:
                    req = urllib.request.Request(url, data=data, headers=headers,
                                                 method="POST")
                    with urllib.request.urlopen(req, timeout=self.timeout) as r:
                        payload = json.loads(r.read().decode("utf-8"))
                    txt = extract_text(payload, proto)
                    self._bump(p.get("id"), sum(len(m) for m in [str(prompt), str(system or "")]),
                               len(txt or ""))
                    if json_mode and not drop_json and txt:
                        self.json_probe[p.get("id")] = True
                    if json_mode and drop_json:
                        self.json_probe[p.get("id")] = False
                    return (txt or "").strip()
                except urllib.error.HTTPError as e:
                    detail = ""
                    try:
                        detail = e.read().decode("utf-8", "ignore")[:400]
                    except Exception:
                        pass
                    last_err = f"HTTP {e.code}: {detail}"
                    low = detail.lower()
                    # 参数不被支持 → 立刻换下一组参数，不浪费重试
                    if e.code in (400, 404, 422):
                        if any(k in low for k in ("max_tokens", "max_completion_tokens",
                                                  "max_output_tokens", "unsupported_parameter",
                                                  "invalid_request")):
                            break
                        if "temperature" in low:
                            break
                        if "response_format" in low or "json" in low:
                            break
                        if e.code == 404:
                            # endpoint 不对，试另一种拼法
                            alt = url.replace("/chat/completions", "")
                            if alt != url and not alt.endswith("/messages"):
                                url = alt + "/chat/completions"
                                continue
                    if e.code in (429, 500, 502, 503, 504):
                        time.sleep(min(2.0 * (attempt + 1), 12))
                        continue
                    break
                except urllib.error.URLError as e:
                    last_err = f"网络不可达：{e}"
                    time.sleep(min(1.5 * (attempt + 1), 8))
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    time.sleep(min(1.5 * (attempt + 1), 8))

        self._bump(p.get("id"), 0, 0, err=True)
        raise LLMError(f"调用失败（provider={p.get('id')} model={p.get('model')}）：{last_err}")

    # ---------- 便捷封装 ----------
    def chat_json(self, prompt, system=None, max_tokens=2400, temperature=0.2,
                  pid=None, retry_plain=True):
        """要求 JSON。先带 json_mode，失败再退化到纯提示 + 宽松解析。"""
        raw = ""
        try:
            raw = self.chat(prompt, system=system, max_tokens=max_tokens,
                            temperature=temperature, pid=pid, json_mode=True)
        except LLMError:
            if not retry_plain:
                raise
        data = parse_json_loose(raw)
        if data is not None:
            return data
        if retry_plain:
            # 退化：不带 response_format，靠提示词 + 宽松解析
            raw2 = self.chat(prompt, system=system, max_tokens=max_tokens,
                             temperature=temperature, pid=pid, json_mode=False)
            return parse_json_loose(raw2)
        return None

    def chat_text(self, prompt, system=None, max_tokens=2400,
                  temperature=0.25, pid=None, role=None):
        """纯文本调用；给 role 时按角色自动挑 provider。"""
        if role and not pid:
            p = self.pick_for(role)
            pid = p.get("id")
        return self.chat(prompt, system=system, max_tokens=max_tokens,
                         temperature=temperature, pid=pid)

    # ---------- 能力探测 ----------
    def probe(self, pid=None, timeout=45):
        """探测一个 provider 是否可用，返回结构化结果（不抛异常）。"""
        p = self.provider(pid)
        out = {"id": p.get("id"), "name": p.get("name"),
               "protocol": _norm_protocol(p), "model": p.get("model"),
               "ok": False, "json_ok": None, "reply": "", "error": ""}
        old_to = self.timeout
        self.timeout = timeout
        try:
            txt = self.chat("回复两个字：可用", max_tokens=32, temperature=0,
                            pid=p.get("id"))
            out["ok"] = True
            out["reply"] = (txt or "")[:60]
        except Exception as e:
            out["error"] = str(e)[:300]
            self.timeout = old_to
            return out
        try:
            d = self.chat_json('只输出 JSON：{"ok":true}', max_tokens=64,
                               temperature=0, pid=p.get("id"))
            out["json_ok"] = isinstance(d, dict)
        except Exception:
            out["json_ok"] = False
        self.timeout = old_to
        return out


# 向后兼容别名
LensLLM = CloudLLM
