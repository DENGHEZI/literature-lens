# -*- coding: utf-8 -*-
"""
文献透镜本地服务（仅标准库，零依赖）

- 托管生成的网页
- /api/health        健康检查（含当前模型、provider 列表、网页路径）
- /api/deepen        按需生成知识点深度讲解（点击时调用，走配置里的模型）
- /api/translate     划词/点击翻译（先查本机翻译缓存，未命中才请求模型 —— 省 Token）
- /api/translate-page 定向翻译：按「阅读指针」提取当前 PDF 指定页原文并整页翻译
- /api/chat          AI 助手多轮对话（只检索与问题相关段落，压缩上下文）
- /api/mindmap       问题导图（同样按相关性取段，控制 Token）
- /api/analyze-pdf   上传 PDF 后台解析：抽取→翻译→创新点分析→写入文献库
- /api/models        查看/切换 provider（配合前端或 curl 使用）

用法：
  python serve.py --data data/lens_data.json --port 8877 --open
  python serve.py --provider kimi          # 临时用别的模型跑深度讲解
"""
import os
import sys
import json
import hashlib
import argparse
import threading
import webbrowser
import re
import time
import uuid
import base64
import tempfile
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from config_loader import load_config, save_user_config
from llm_client import CloudLLM, LLMError, _norm_protocol
from analyzer import Analyzer
from web_egress import apply_proxy, web_search, translation_confidence

STATE = {"data": None, "llm": None, "az": None, "html": "",
         "doc_index": {}, "html_path": "", "data_path": "", "cfg": {},
         "upload_dir": ""}

# 上传解析任务表 + 数据文件写锁
TASKS = {}
_TASKS_LOCK = threading.Lock()
_DATA_LOCK = threading.Lock()

# ---------------- 翻译缓存（省 Token 的第一道闸） ----------------
_ROOT = os.path.dirname(HERE)
_TR_PATH = os.path.join(_ROOT, "data", "translate_cache.json")
_TR_CACHE = {}
_TR_LOCK = threading.Lock()


def _load_tr_cache():
    if _TR_CACHE:
        return
    try:
        with open(_TR_PATH, encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, dict):
                _TR_CACHE.update(d)
    except Exception:
        pass


def _tr_key(text, mode):
    h = hashlib.sha1((mode + "\x00" + text).encode("utf-8")).hexdigest()[:20]
    return h


def _tr_get(key):
    _load_tr_cache()
    with _TR_LOCK:
        return _TR_CACHE.get(key)


def _tr_put(key, val):
    _load_tr_cache()
    with _TR_LOCK:
        _TR_CACHE[key] = val
        try:
            if len(_TR_CACHE) > 6000:          # 防膨胀：超过 6000 条时丢弃最早的 1000 条
                for k in list(_TR_CACHE)[:1000]:
                    _TR_CACHE.pop(k, None)
            os.makedirs(os.path.dirname(_TR_PATH), exist_ok=True)
            with open(_TR_PATH, "w", encoding="utf-8") as f:
                json.dump(_TR_CACHE, f, ensure_ascii=False)
        except Exception:
            pass


def _translate_text(text, mode):
    """带落盘缓存的翻译：命中直接返回；未命中才请求模型并写缓存。

    返回 (translation, cached, error)；error 非 None 时前两项为 None。
    供 /api/translate（划选）与 /api/translate-page（整页定向翻译）共用。
    """
    cache_key = _tr_key(text, mode)
    cached = _tr_get(cache_key)
    if cached:
        return cached, True, None
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    is_en2zh = cjk < len(text) * 0.5      # 英文为主 → 翻成中文
    if is_en2zh:
        if mode == "academic":
            prompt = ("把下面的英文学术片段准确翻译成简体中文。要求保留专业术语的规范中文译名、"
                      "数字符号与公式原样；学术语体，避免口语化。只输出译文，不要解释。\n\n" + text)
        else:
            prompt = ("把下面的英文学术片段准确翻译成简体中文。保留专业术语的规范译名"
                      "与数字符号；只输出译文，不要解释。\n\n" + text)
    else:
        prompt = ("Translate the following Chinese academic passage into "
                  "accurate, idiomatic English. Keep technical terms "
                  "standard. Output the translation only.\n\n" + text)
    try:
        out = STATE["llm"].chat_text(prompt, temperature=0.15, max_tokens=2000)
    except LLMError as e:
        return None, False, f"{e}"[:220]
    except Exception as e:
        return None, False, f"{type(e).__name__}: {e}"[:200]
    if not out:
        return None, False, "模型未返回有效内容"
    _tr_put(cache_key, out.strip())
    return out.strip(), False, None


def _page_text_from_pdf(pdf_path, page_no):
    """用 fitz 从 PDF 原文件提取指定页（1 起始）的文本；失败返回空串。"""
    try:
        import fitz
        with fitz.open(pdf_path) as d:
            if 1 <= page_no <= d.page_count:
                return d[page_no - 1].get_text() or ""
    except Exception:
        pass
    return ""


# 页词坐标缓存：{doc_id: {page: [[x0,y0,x1,y1,word], ...]}}（0-1000 归一化）
_WORDS_CACHE = {}
_WORDS_LOCK = threading.Lock()


def _words_cache_get(doc_id, doc, page_no, max_pages=80):
    """取指定页的单词坐标层；upload 文献从原始 PDF 实时提取并缓存。

    返回 [[x0,y0,x1,y1,word],...]（数值 0-1000，前端 ÷10 即百分比），
    提不到时返回 None。
    """
    with _WORDS_LOCK:
        hit = _WORDS_CACHE.get(doc_id, {}).get(page_no)
    if hit is not None:
        return hit
    words = None
    meta = doc.get("meta") or {}
    if meta.get("source_key") == "upload":
        pdf = _resolve_upload_pdf(doc)
        if pdf:
            words = _page_words_from_pdf(pdf, page_no)
    else:
        pt = doc.get("pages_txt") or {}
        v = pt.get(str(page_no)) or pt.get(page_no)
        if isinstance(v, list) and v and isinstance(v[0], (list, tuple)):
            words = v
    if words:
        with _WORDS_LOCK:
            _WORDS_CACHE.setdefault(doc_id, {})
            if len(_WORDS_CACHE[doc_id]) > max_pages:
                for k in list(_WORDS_CACHE[doc_id])[:20]:
                    _WORDS_CACHE[doc_id].pop(k, None)
            _WORDS_CACHE[doc_id][page_no] = words
    return words


def _page_words_from_pdf(pdf_path, page_no):
    """fitz 提取指定页单词框，按页宽高归一化到 0-1000；失败返回 None。"""
    try:
        import fitz
        with fitz.open(pdf_path) as d:
            if not (1 <= page_no <= d.page_count):
                return None
            pg = d[page_no - 1]
            w, h = pg.rect.width, pg.rect.height
            if not w or not h:
                return None
            out = []
            for x0, y0, x1, y1, word, *_ in pg.get_text("words"):
                if not str(word).strip():
                    continue
                out.append([round(x0 / w * 1000), round(y0 / h * 1000),
                            round(x1 / w * 1000), round(y1 / h * 1000), word])
            return out or None
    except Exception:
        return None


def _page_image(doc_id, doc, page_no, zoom=1.6, quality=78):
    """按需渲染指定页为 JPEG（落盘缓存 data/pages/<doc_id>/pN.jpg）；失败返回 None。

    上传文献解析时只预渲染前几页页图，超出部分由这里实时补齐，
    保证页图模式覆盖整本 PDF。
    zoom 1.6 / 质量 78：手机屏 DPR 下文字依然清晰，体积约为 zoom2.0/q85 的一半，
    局域网/真机加载明显更快。
    """
    try:
        cache_dir = os.path.join(os.path.dirname(STATE["data_path"]), "pages", doc_id)
        fp = os.path.join(cache_dir, f"p{page_no}.jpg")
        if os.path.isfile(fp) and os.path.getsize(fp) > 100:
            return fp
        pdf = _resolve_upload_pdf(doc)
        if not pdf:
            return None
        import fitz
        with fitz.open(pdf) as d:
            if not (1 <= page_no <= d.page_count):
                return None
            pix = d[page_no - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            os.makedirs(cache_dir, exist_ok=True)
            fp_tmp = fp + ".tmp"
            data = pix.tobytes("jpeg", jpg_quality=quality)
            with open(fp_tmp, "wb") as f:
                f.write(data)
            os.replace(fp_tmp, fp)
        return fp if os.path.isfile(fp) else None
    except Exception:
        return None


def _extract_kws(question):
    """把问题拆成关键词（中英文混排），供相关性检索取段。"""
    kws = []
    for w in re.split(r"[^\w\u4e00-\u9fff]+", question or ""):
        w = w.strip().lower()
        if len(w) >= 2 and w not in kws:
            kws.append(w)
    return kws


def _relevant_ctx(doc, question, max_chars=5000, max_blocks=14):
    """按关键词命中率挑与问题最相关的段落（代替“前 N 段全塞”）。

    省 Token 的关键：思维导图 / 对话只看与问题相关的 12-14 段，
    上下文从 10000+ 字压缩到 ~5000 字，且证据更准（不会只引开头）。
    一段都没命中时回退到“头+中+尾”均匀采样，保证仍有可用语境。
    """
    kws = _extract_kws(question)
    blocks = doc.get("bilingual") or []
    if not blocks:
        return ""
    if not kws:
        scored = [(0, i, b) for i, b in enumerate(blocks)]
    else:
        scored = []
        for i, b in enumerate(blocks):
            src = (b.get("source") or "").lower()
            s = sum(src.count(k) for k in kws)
            scored.append((s, i, b))
    hits = [x for x in scored if x[0] > 0]
    if hits:
        hits.sort(key=lambda x: (-x[0], x[1]))
        picked = sorted(hits[:max_blocks], key=lambda x: x[1])
    else:
        n = len(scored)
        step = max(1, n // max_blocks)
        picked = scored[::step][:max_blocks]
    parts, used = [], 0
    for s, i, b in picked:
        t = f"(p{b.get('page','')}) {b.get('source','')}"
        if used + len(t) > max_chars and parts:
            break
        parts.append(t)
        used += len(t)
    return "\n\n".join(parts)


def _norm_fp(s):
    """归一化文本指纹：只留中英文与数字，截前 400 字。"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (s or "").lower())[:400]


def _pdf_fingerprint(path, max_pages=2):
    """取 PDF 前两页文本指纹（用于把旧版上传文献匹配回 uploads 里的原始文件）。"""
    try:
        import fitz
        with fitz.open(path) as d:
            t = ""
            for i in range(min(max_pages, d.page_count)):
                t += d[i].get_text()
        return _norm_fp(t)
    except Exception:
        return ""


def _doc_fingerprint(doc):
    parts = []
    for b in (doc.get("bilingual") or [])[:4]:
        parts.append(b.get("source") or "")
        if sum(len(p) for p in parts) > 600:
            break
    return _norm_fp(" ".join(parts))


def _resolve_upload_pdf(doc):
    """找到上传文献对应的原始 PDF（内置 PDF 阅读器的数据源）。

    新版上传：meta.upload_id 直接命中。
    旧版上传（没存 upload_id）：用正文指纹在 uploads/ 里找回，并写回数据文件，
    下次直接命中。
    """
    meta = doc.get("meta") or {}
    up = str(meta.get("upload_id") or "")
    if re.fullmatch(r"[0-9a-f]{32}", up):
        fp = os.path.join(STATE["upload_dir"], up + ".pdf")
        if os.path.isfile(fp):
            return fp
    doc_fp = _doc_fingerprint(doc)
    if len(doc_fp) < 60:
        return None
    import difflib
    best, best_score = None, 0.0
    try:
        names = os.listdir(STATE["upload_dir"])
    except Exception:
        return None
    for n in names:
        if not re.fullmatch(r"[0-9a-f]{32}\.pdf", n):
            continue
        fp = os.path.join(STATE["upload_dir"], n)
        pdf_fp = _pdf_fingerprint(fp)
        if not pdf_fp:
            continue
        score = difflib.SequenceMatcher(None, doc_fp[:300], pdf_fp[:300]).ratio()
        if len(doc_fp) >= 120 and doc_fp[:120] in pdf_fp:
            score = max(score, 0.95)
        if score > best_score:
            best, best_score = fp, score
    if best and best_score >= 0.55:
        up2 = os.path.basename(best)[:-4]
        doc.setdefault("meta", {})["upload_id"] = up2
        try:
            with _DATA_LOCK:
                raw = json.load(open(STATE["data_path"], encoding="utf-8"))
                for d in raw.get("docs") or []:
                    if d.get("id") == doc.get("id"):
                        d.setdefault("meta", {})["upload_id"] = up2
                        break
                tmp = STATE["data_path"] + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(raw, f, ensure_ascii=False, indent=1)
                os.replace(tmp, STATE["data_path"])
        except Exception:
            pass
        return best
    return None


def _build_temporary_llm(req):
    """根据前端表单的临时配置,构建一个不入 STATE 的 CloudLLM(只用于探测/试用)。

    支持三种载荷:
      - {"base_url","model","api_key","protocol"?}    单 provider 模式
      - {"translation":{...}, "reasoning":{...}}       双 provider 模式(测第一个)
      - {"preset_id":"deepseek","api_key":"..."}       预设 + 仅给 Key
    """
    from llm_client import CloudLLM
    base = str(req.get("base_url") or "").strip().rstrip("/")
    model = str(req.get("model") or "").strip()
    key = str(req.get("api_key") or "").strip()
    proto = (req.get("protocol") or "").strip() or None
    if not base:
        # 尝试从双 provider 模式抽出第一个
        if isinstance(req.get("translation"), dict):
            tr = req["translation"]
            base = str(tr.get("base_url") or "").strip().rstrip("/")
            model = str(tr.get("model") or "").strip()
            key = str(tr.get("api_key") or "").strip()
            proto = (tr.get("protocol") or "").strip() or None
    if not (base and model and key):
        raise ValueError("缺少 base_url/model/api_key 之一")
    provider = {
        "id": "tmp-probe", "name": "临时探测", "protocol": proto or "openai",
        "base_url": base, "api_key": key, "model": model, "enabled": True,
        "weight": 10, "roles": ["chat", "translate", "analyze", "mindmap"],
        "ctx": 128000, "max_output": 8192, "supports_json": True,
    }
    return CloudLLM([provider], active="tmp-probe")


# 预设服务商清单(只用于前端回填,key 必须由用户自己填)。
# 与 scripts/llm_models.PRESETS 字段对齐:protocol/base_url/model。
API_PRESETS = [
    {"id": "deepseek",  "name": "DeepSeek",    "protocol": "openai",    "base_url": "https://api.deepseek.com/v1",                                 "model": "deepseek-chat"},
    {"id": "glm",       "name": "智谱 GLM",    "protocol": "openai",    "base_url": "https://open.bigmodel.cn/api/paas/v4",                      "model": "glm-4-plus"},
    {"id": "kimi",      "name": "月之暗面 Kimi", "protocol": "openai",  "base_url": "https://api.moonshot.cn/v1",                                "model": "moonshot-v1-128k"},
    {"id": "openai",    "name": "OpenAI",      "protocol": "openai",    "base_url": "https://api.openai.com/v1",                                  "model": "gpt-4o-mini"},
    {"id": "anthropic", "name": "Anthropic Claude", "protocol": "anthropic", "base_url": "https://api.anthropic.com/v1",                         "model": "claude-sonnet-4-20250514"},
    {"id": "gemini",    "name": "Google Gemini", "protocol": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",           "model": "gemini-2.0-flash"},
    {"id": "qwen",      "name": "通义千问",    "protocol": "openai",    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",         "model": "qwen-plus"},
    {"id": "doubao",    "name": "豆包",        "protocol": "openai",    "base_url": "https://ark.cn-beijing.volces.com/api/v3",                   "model": "doubao-1-5-pro-32k-250115"},
    {"id": "silicon",   "name": "硅基流动",    "protocol": "openai",    "base_url": "https://api.siliconflow.cn/v1",                              "model": "Qwen/Qwen2.5-72B-Instruct"},
    {"id": "openrouter","name": "OpenRouter",  "protocol": "openai",    "base_url": "https://openrouter.ai/api/v1",                               "model": "openai/gpt-4o-mini"},
    {"id": "custom",    "name": "自定义 / 其他",   "protocol": "openai", "base_url": "",                                                          "model": ""},
]


def _normalize_settings_payload(req):
    """把前端发来的 settings 载荷统一转成 {providers, active, message}。

    支持三种形式(按优先级):
      1. preset + api_key           从预设库挑,只填 Key,极简路径
      2. single: {base_url, model, api_key, protocol?}  单 API 模式
      3. translation + reasoning    双 API 模式(原有)
    """
    # ---- 1. 预设模式(极简) ----
    if req.get("preset") and req.get("api_key"):
        pid = str(req.get("preset")).strip()
        preset = next((p for p in API_PRESETS if p["id"] == pid and p["id"] != "custom"), None)
        if not preset:
            raise ValueError(f"未知预设:{pid}")
        key = str(req.get("api_key") or "").strip()
        if not key:
            raise ValueError("请填写 API Key")
        prov = {
            "id": "user-api",
            "name": "用户 API",
            "protocol": preset["protocol"],
            "base_url": preset["base_url"],
            "api_key": key,
            "model": req.get("model") or preset["model"],
            "enabled": True, "weight": 20,
            "roles": ["chat", "translate", "analyze", "mindmap"],
            "ctx": 128000, "max_output": 8192, "supports_json": True,
        }
        return {
            "providers": [prov],
            "active": "user-api",
            "message": f"已启用 {preset['name']} · {prov['model']}。可立即使用翻译、AI 助手与思维导图。"
        }

    # ---- 2. 单 API 模式 ----
    if isinstance(req.get("single"), dict):
        raw = req["single"]
        base = str(raw.get("base_url") or "").strip().rstrip("/")
        model = str(raw.get("model") or "").strip()
        key = str(raw.get("api_key") or "").strip()
        if not (base and model and key):
            raise ValueError("请填写 API 地址 / 模型名 / API Key")
        u = urlparse(base)
        if not (u.scheme in ("http", "https") and u.netloc):
            raise ValueError("API 地址必须以 http:// 或 https:// 开头")
        prov = {
            "id": "user-api", "name": "用户 API",
            "protocol": raw.get("protocol") or "openai",
            "base_url": base, "api_key": key, "model": model,
            "enabled": True, "weight": 20,
            "roles": ["chat", "translate", "analyze", "mindmap"],
            "ctx": int(raw.get("ctx") or 128000),
            "max_output": int(raw.get("max_output") or 8192),
            "supports_json": True,
        }
        return {
            "providers": [prov],
            "active": "user-api",
            "message": f"已启用用户单 API · {model}。可立即使用翻译、AI 助手与思维导图。"
        }

    # ---- 3. 双 API 模式(向后兼容) ----
    if isinstance(req.get("translation"), dict) or isinstance(req.get("reasoning"), dict):
        def make_provider(raw, ident, name, roles):
            raw = raw if isinstance(raw, dict) else {}
            base = str(raw.get("base_url") or "").strip().rstrip("/")
            model = str(raw.get("model") or "").strip()
            key = str(raw.get("api_key") or "").strip()
            u = urlparse(base)
            if not (u.scheme in ("http", "https") and u.netloc):
                raise ValueError(f"{name} API 地址必须以 http:// 或 https:// 开头")
            if not model:
                raise ValueError(f"请填写{name}模型名")
            if not key:
                raise ValueError(f"请填写{name} API Key")
            return {"id": ident, "name": name, "protocol": raw.get("protocol") or "openai",
                    "base_url": base, "api_key": key, "model": model, "enabled": True,
                    "weight": 20 if ident == "user-reasoning" else 10, "roles": roles,
                    "ctx": int(raw.get("ctx") or 128000),
                    "max_output": int(raw.get("max_output") or 8192),
                    "supports_json": True}
        tr = make_provider(req.get("translation"), "user-translation", "学术翻译", ["chat", "translate"])
        rs_raw = req.get("reasoning") or req.get("translation")
        rs = make_provider(rs_raw, "user-reasoning", "科研助手 / 导图", ["analyze", "mindmap"])
        return {
            "providers": [tr, rs],
            "active": "user-translation",
            "message": "已分别接入翻译 / 科研助手双 API。"
        }

    raise ValueError("请在表单中填写 API 信息,或选择一个预设")


def load(data_path, html_path=None, cfg=None):
    cfg = cfg or load_config()
    STATE["cfg"] = cfg
    STATE["data_path"] = data_path
    STATE["data"] = json.load(open(data_path, encoding="utf-8"))
    STATE["doc_index"] = {d["id"]: d for d in STATE["data"]["docs"]}
    STATE["llm"] = CloudLLM(cfg.get("providers"), active=cfg.get("active_provider"))
    STATE["az"] = Analyzer(STATE["llm"])
    STATE["html_path"] = html_path or ""
    if html_path and os.path.exists(html_path):
        STATE["html"] = open(html_path, encoding="utf-8").read()
    upload_cfg = cfg.get("uploads") or {}
    STATE["upload_dir"] = os.path.abspath(upload_cfg.get("dir") or
                                            os.path.join(os.path.dirname(HERE), "uploads"))
    os.makedirs(STATE["upload_dir"], exist_ok=True)


def _set_task(task_id, **kw):
    with _TASKS_LOCK:
        TASKS.setdefault(task_id, {}).update(kw)


def _analyze_worker(task_id, upload_id, filename, llm_cfg=None):
    """后台解析上传的 PDF：抽取 → 术语 → 批量翻译 → 创新点分析 → 写入文献库。

    复用主流水线（pipeline.process_one），与离线批量分析产出同一套数据结构，
    因此前端速览卡 / 译文 / 原文页 / 创新点 / 知识点全部立即生效。
    """
    def set_task(**kw):
        _set_task(task_id, **kw)
    try:
        from pipeline import process_one
        from translator import Translator
        pdf_path = os.path.join(STATE["upload_dir"], upload_id + ".pdf")
        if not os.path.isfile(pdf_path):
            raise FileNotFoundError("上传文件已不存在，请重新上传")
        set_task(status="running", stage="正在抽取正文与原页图…")
        rec = {"filename": filename, "path": pdf_path}
        llm = STATE["llm"]
        # 优先用前端（手机）随请求传来的临时 Key；否则回退后端已配置的 provider。
        # => 云端后端无需任何 AI 配置，PDF 解析直接用用户手机上已配好的 Key。
        if llm_cfg:
            try:
                llm = _build_temporary_llm(llm_cfg)
            except Exception:
                llm = STATE["llm"]
        has_llm = bool(llm and getattr(llm, "providers", None))
        tr = Translator(llm) if has_llm else None
        az = Analyzer(llm) if has_llm else None
        if not has_llm:
            set_task(status="error", stage="解析失败",
                     error="后端未配置 AI 模型且前端未提供 Key；请在「设置 → API」填写 Key")
            return
        doc = process_one(rec, llm, tr, az, max_pages=8)
        doc["meta"]["source"] = "本地上传"
        doc["meta"]["source_key"] = "upload"
        doc["meta"]["upload_id"] = upload_id   # 内置 PDF 阅读器按此找回原始文件
        set_task(stage="正在写入文献库…")
        # 写回数据文件（带锁，防止与其它任务并发写坏）
        with _DATA_LOCK:
            raw = json.load(open(STATE["data_path"], encoding="utf-8"))
            raw["docs"] = [d for d in (raw.get("docs") or []) if d.get("id") != doc["id"]]
            raw["docs"].append(doc)
            tmp = STATE["data_path"] + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=1)
            os.replace(tmp, STATE["data_path"])
        STATE["doc_index"][doc["id"]] = doc
        STATE["data"]["docs"] = [d for d in (STATE["data"].get("docs") or [])
                                 if d.get("id") != doc["id"]]
        STATE["data"]["docs"].append(doc)
        # 给前端的副本：pages_img 换成可访问的服务端 URL
        out = json.loads(json.dumps(doc, ensure_ascii=False))
        out["pages_img"] = {str(k): f"data/pages/{doc['id']}/p{k}.jpg"
                            for k in (doc.get("pages_img") or {})}
        set_task(status="done", stage="完成", doc=out, doc_id=doc["id"])
        # 后台重渲染离线网页（双击打开也能看到新文献）
        try:
            import render as _render
            hp = STATE["html_path"] or None
            if hp:
                _render.render(STATE["data_path"], hp)
        except Exception:
            pass
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:260]
        low = msg.lower()
        if "401" in msg or "api key" in low or "unauthorized" in low or "authentication" in low:
            msg += " ｜ 提示：请先到「设置 → API」粘贴有效 Key，点「测试连接」确认后再上传"
        set_task(status="error", stage="解析失败", error=msg)


def _reanalyze_worker(task_id, doc_id):
    """重新解析已有文献：重跑 抽取→术语→翻译→创新点分析，并保持原 doc id 不变。

    用途：上传时 API Key 失效导致速览卡/译文为空，配好 Key 后一键补救；
    也支持把文献卡片拖进 AI 对话框触发分析。
    """
    def set_task(**kw):
        _set_task(task_id, **kw)
    try:
        from pipeline import process_one
        from translator import Translator
        old = STATE["doc_index"].get(doc_id)
        if not old:
            raise FileNotFoundError("文献不存在或已被移除")
        pdf_path = _resolve_upload_pdf(old)
        if not pdf_path:
            raise ValueError("该文献没有原始 PDF（目前仅「本地上传」的文献支持重新解析）")
        set_task(status="running", stage="正在抽取正文与原页图…")
        rec = {"filename": (old.get("meta") or {}).get("filename") or "",
               "path": pdf_path}
        tr = Translator(STATE["llm"])
        doc = process_one(rec, STATE["llm"], tr, STATE["az"], max_pages=8)
        doc["id"] = doc_id                      # 保持原 id，前端选中所见即所得
        old_meta = old.get("meta") or {}
        doc["meta"]["source"] = old_meta.get("source") or "本地上传"
        doc["meta"]["source_key"] = "upload"
        if old_meta.get("upload_id"):
            doc["meta"]["upload_id"] = old_meta["upload_id"]
        set_task(stage="正在写入文献库…")
        with _DATA_LOCK:
            raw = json.load(open(STATE["data_path"], encoding="utf-8"))
            raw["docs"] = [d for d in (raw.get("docs") or [])
                           if d.get("id") != doc["id"]]
            raw["docs"].append(doc)
            tmp = STATE["data_path"] + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=1)
            os.replace(tmp, STATE["data_path"])
        STATE["doc_index"][doc["id"]] = doc
        STATE["data"]["docs"] = [d for d in (STATE["data"].get("docs") or [])
                                 if d.get("id") != doc["id"]]
        STATE["data"]["docs"].append(doc)
        out = json.loads(json.dumps(doc, ensure_ascii=False))
        out["pages_img"] = {str(k): f"data/pages/{doc['id']}/p{k}.jpg"
                            for k in (doc.get("pages_img") or {})}
        set_task(status="done", stage="完成", doc=out, doc_id=doc["id"])
        try:
            import render as _render
            if STATE["html_path"]:
                _render.render(STATE["data_path"], STATE["html_path"])
        except Exception:
            pass
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:260]
        low = msg.lower()
        if "401" in msg or "api key" in low or "unauthorized" in low or "authentication" in low:
            msg += " ｜ 提示：请先到「设置 → API」粘贴有效 Key 并测试连接"
        set_task(status="error", stage="解析失败", error=msg)


# ---------------- 微信云存储取文件（callContainer 下 PDF 走对象存储） ----------------
_WX_TOKEN = {"token": "", "exp": 0}


_WX_SECRET_FILE = None


def _load_wx_secret_file():
    """读取部署目录下的 wx_secret.json（{"WX_APPID":..,"WX_SECRET":..,"WX_ENV":..}）。

    作用：把 WX 密钥直接打进部署包，免去在云托管控制台手填环境变量。
    环境变量优先；该文件仅作兜底。文件不应提交到 git（.gitignore 已忽略）。
    """
    global _WX_SECRET_FILE
    if _WX_SECRET_FILE is not None:
        return _WX_SECRET_FILE
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "..", "wx_secret.json"),
                 os.path.join(here, "wx_secret.json"),
                 "wx_secret.json"):
        try:
            if cand and os.path.exists(cand):
                with open(cand, "r", encoding="utf-8") as f:
                    _WX_SECRET_FILE = json.load(f) or {}
                return _WX_SECRET_FILE
        except Exception:
            continue
    _WX_SECRET_FILE = {}
    return _WX_SECRET_FILE


def _read_cloudbase_token():
    """微信云托管容器会自动把 access_token 推送到该只读挂载文件（约10分钟刷新，30分钟有效）。

    存在说明运行在云托管环境，可免 AppSecret 调用开放接口（需控制台「微信令牌权限配置」添加对应接口路径）。
    """
    candidates = (
        "/.tencentcloudbase/wx/cloudbase_access_token",
        os.path.join(os.environ.get("TENCENTCLOUD_RUNENV_BASE", ""), "wx/cloudbase_access_token"),
    )
    for p in candidates:
        if not p:
            continue
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    t = (f.read() or "").strip()
                if t:
                    return t
        except Exception:
            continue
    return None


def _env_from_fileid(fileid):
    """从云存储 fileID 自动解析环境 ID。

    fileID 形如 cloud://<env-id>.<bucket-后缀>/<路径>，
    例：cloud://prod-d1gfs45gs080943ba.7065-prod-xxx-1258717764/lens_uploads/a.pdf
    → 环境ID = prod-d1gfs45gs080943ba。免去手配 WX_ENV 环境变量。
    """
    try:
        s = (fileid or "").strip()
        if s.lower().startswith("cloud://"):
            first = s[8:].split("/", 1)[0]
            env = first.split(".", 1)[0].strip()
            if env:
                return env
    except Exception:
        pass
    return ""


def _wx_secret_access_token():
    """仅用 AppID + AppSecret 换取 access_token；无配置时返回 None（不抛错）。"""
    appid = (os.environ.get("WX_APPID") or "").strip()
    secret = (os.environ.get("WX_SECRET") or "").strip()
    if not (appid and secret):
        ws = _load_wx_secret_file() or {}
        appid = appid or str(ws.get("WX_APPID") or "").strip()
        secret = secret or str(ws.get("WX_SECRET") or "").strip()
    if not (appid and secret):
        return None
    now = time.time()
    if _WX_TOKEN["token"] and _WX_TOKEN["exp"] > now + 300:
        return _WX_TOKEN["token"]
    url = ("https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential"
           f"&appid={urllib.parse.quote(appid)}&secret={urllib.parse.quote(secret)}")
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if "access_token" not in d:
        return None
    _WX_TOKEN["token"] = d["access_token"]
    _WX_TOKEN["exp"] = now + int(d.get("expires_in", 7200))
    return _WX_TOKEN["token"]


def _wx_file_download_url(fileid, env=None):
    """把云存储 fileID 换成临时下载地址（tcb/batchdownloadfile）。

    凭证优先级（免 AppSecret 优先）：
      1) 云托管自动推送令牌 cloudbase_access_token（容器挂载文件，需控制台「微信令牌权限配置」添加 /tcb/batchdownloadfile）
      2) 开放接口服务：不带 token 直接调用（同样需上面权限配置）
      3) 小程序 AppSecret（环境变量 / wx_secret.json）
    """
    env = (env or os.environ.get("WX_ENV") or "").strip()
    if not env:
        env = str((_load_wx_secret_file() or {}).get("WX_ENV") or "").strip()
    if not env:
        # fileID 自带环境 ID（cloud://<env>.<bucket>/<path>），免配置直接解析
        env = _env_from_fileid(fileid)
    if not env:
        raise RuntimeError("缺少云环境 ID（WX_ENV 未配置，且 fileID 无法解析出环境 ID）")

    body = json.dumps({"env": env, "file_list": [{"fileid": fileid, "max_age": 7200}]}).encode("utf-8")

    attempts = []
    cb = _read_cloudbase_token()
    if cb:
        attempts.append(("cloudbase_access_token", cb))
    attempts.append((None, None))  # 开放接口服务：不带 token
    sec = _wx_secret_access_token()
    if sec:
        attempts.append(("access_token", sec))

    last_err = None
    for param, token in attempts:
        if param:
            api = f"https://api.weixin.qq.com/tcb/batchdownloadfile?{param}=" + token
        else:
            api = "https://api.weixin.qq.com/tcb/batchdownloadfile"
        req = urllib.request.Request(api, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last_err = f"请求异常：{e}"
            continue
        if d.get("errcode"):
            # 41001/40014 等表示此路不通，换下一种凭证
            last_err = f"errcode={d.get('errcode')} {d.get('errmsg')}"
            continue
        lst = d.get("file_list") or d.get("download_list") or []
        if not lst:
            last_err = "返回文件列表为空：" + str(d)
            continue
        item = lst[0] or {}
        dl = item.get("download_url") or item.get("url")
        if not dl:
            last_err = "返回无 download_url：" + str(item)
            continue
        return dl
    raise RuntimeError("获取文件下载地址失败（已尝试云托管令牌/开放接口服务/AppSecret）：" + str(last_err))


# ---------------- 大模型请求统一处理（同步 / 异步复用） ----------------
_LLM_STAGE = {
    "translate": "正在翻译…",
    "translate_page": "正在整页翻译…",
    "chat": "AI 思考中…",
    "mindmap": "正在生成思维导图…",
    "deepen": "正在生成深度讲解…",
}


def _llm_translate(text, mode, with_confidence=False):
    """带落盘缓存的翻译，返回 (result_dict, error_str)。"""
    translation, cached, err = _translate_text(text, mode)
    if err:
        return None, err
    confidence = None
    if with_confidence:
        try:
            confidence = translation_confidence(
                text, translation, STATE["cfg"].get("egress"),
                timeout=(STATE["cfg"].get("egress") or {}).get("search_timeout", 10))
        except Exception:
            confidence = None
    return {"translation": translation, "mode": mode,
            "cached": cached, "confidence": confidence}, None


def _handle_llm_action(action, req):
    """统一执行各类大模型请求，返回 (result_dict, error_str)。

    供同步端点（uni.request 模式）与异步任务端点（callContainer ≤15s 模式）共用。
    两种接入方式结果一致；error 非 None 时 result 为 None。
    """
    llm = STATE["llm"]
    if not llm or not getattr(llm, "providers", None):
        return None, ("后端未配置 AI 模型。请在网页「设置→API」粘贴 Key，"
                      "或在小程序「我的」里自行配置各端 API。")
    req = req or {}
    if action == "translate":
        text = (req.get("text") or "").strip()[:4000]
        if not text:
            return None, "text 为空"
        return _llm_translate(text, (req.get("mode") or "general").lower(),
                              bool(req.get("with_confidence")))
    if action == "translate_page":
        doc_id = str(req.get("doc") or "")
        try:
            page = int(req.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
        doc = STATE["doc_index"].get(doc_id)
        if doc is None or page < 1:
            return None, "doc 或 page 参数无效"
        text = ""
        if (doc.get("meta") or {}).get("source_key") == "upload":
            pdf = _resolve_upload_pdf(doc)
            if pdf:
                text = _page_text_from_pdf(pdf, page)
        if not text:
            pt = doc.get("pages_txt") or {}
            text = pt.get(str(page)) or pt.get(page) or ""
        text = (text or "").strip()
        if not text:
            return None, f"第 {page} 页没有可提取的文本（扫描版 PDF 或超出页码范围）"
        chars = len(text)
        r, err = _llm_translate(text, "academic", bool(req.get("with_confidence")))
        if err:
            return None, err
        r["page"] = page
        r["chars"] = chars
        return r, None
    if action == "chat":
        doc = STATE["doc_index"].get(req.get("doc_id"))
        if not doc:
            return None, "请先从左侧选择一篇文献"
        messages = req.get("messages") or []
        if not isinstance(messages, list) or not messages:
            return None, "messages 不能为空"
        trimmed = []
        for m in messages[-8:]:
            if not isinstance(m, dict):
                continue
            role = (m.get("role") or "").strip().lower()
            if role not in ("user", "assistant", "system"):
                continue
            content = str(m.get("content") or "").strip()[:2000]
            if content:
                trimmed.append({"role": role, "content": content})
        if not trimmed:
            return None, "没有有效消息"
        last_user = ""
        for m in reversed(trimmed):
            if m["role"] == "user":
                last_user = m["content"]
                break
        ctx = _relevant_ctx(doc, last_user or (trimmed[-1]["content"] if trimmed else ""))
        profile = doc.get("profile") or {}
        profile_text = json.dumps({
            "title_zh": profile.get("title_zh", ""),
            "research_question": profile.get("research_question", ""),
            "method": profile.get("method", ""),
            "findings": profile.get("findings", []),
            "keywords": profile.get("keywords", []),
        }, ensure_ascii=False)
        field = (req.get("field") or "").strip()
        field_line = (f"\n【提问者研究方向】{field}\n"
                      "对方可能是该领域的科研工作者，请用其熟悉的专业语言作答。"
                      if field else "")
        ptr = req.get("pointer") or {}
        ptr_line = ""
        if isinstance(ptr, dict) and ptr.get("page"):
            ptr_line = (f"\n【阅读指针】用户正在阅读原文第 {ptr.get('page')} 页，"
                        "回答时优先结合该页的内容与图表。")
        ctx_block = ("【文献标题】" + (doc["meta"].get("title") or "") + "\n"
                     "【文献画像】" + profile_text[:1500] + "\n"
                     "【正文片段（已按问题相关性筛选）】\n"
                     + ctx[:10000] + field_line + ptr_line)
        system_prompt = ("你是文献透镜的 AI 科研助手，擅长根据当前论文回答用户的问题。"
                         "回答必须基于以下文献语境；无法确认时明确写「原文未提及」，绝不编造。"
                         "回复简洁有层次，优先用中文，长度控制在 600 字以内。\n\n" + ctx_block)
        history = [{"role": "system", "content": system_prompt}] + trimmed
        try:
            reply = llm.chat(history[-1]["content"], system=system_prompt,
                             temperature=0.25, max_tokens=1500)
        except LLMError as e:
            return None, f"{e}"[:220]
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"[:200]
        if not reply:
            return None, "模型未返回有效内容"
        pinfo = llm.provider()
        return {"reply": str(reply).strip(), "model": pinfo.get("name", ""),
                "usage_est": {
                    "in_chars": sum(len(m["content"]) for m in trimmed) + len(system_prompt),
                    "out_chars": len(reply.strip())}}, None
    if action == "mindmap":
        question = (req.get("question") or "").strip()
        doc = STATE["doc_index"].get(req.get("doc_id"))
        if not question:
            return None, "请先输入想理解的问题"
        if not doc:
            return None, "请先从左侧选择一篇文献"
        ctx = _relevant_ctx(doc, question)
        result = STATE["az"].question_map(question, ctx, doc.get("profile") or {})
        if not result:
            return None, "导图生成失败；请检查用户提供的推理 API 或稍后重试"
        pinfo = llm.pick_for("mindmap") or llm.provider()
        return {"mindmap": result, "model": pinfo.get("name") or pinfo.get("id", "")}, None
    if action == "deepen":
        doc = STATE["doc_index"].get(req.get("doc_id"))
        cid = req.get("concept_id")
        if not doc:
            return None, "文献不存在"
        concept = next((c for c in doc.get("concepts", []) if c["id"] == cid), None)
        if not concept:
            return None, "知识点不存在"
        ctx = "\n\n".join(b["source"] for b in doc.get("bilingual", [])[:40])
        try:
            d = STATE["az"].deepen(concept, ctx)
        except LLMError as e:
            return None, f"{e}"[:220]
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"[:200]
        if not d:
            return None, "模型未返回有效内容"
        return {"detail": d, "model": llm.provider()["name"]}, None
    return None, f"未知 action：{action}"


def _llm_worker(task_id, action, req):
    """后台跑大模型请求，结果写入 TASKS（callContainer ≤15s 异步化）。"""
    def set_task(**kw):
        _set_task(task_id, **kw)
    try:
        set_task(status="running", stage=_LLM_STAGE.get(action, "AI 生成中…"))
        result, err = _handle_llm_action(action, req)
        if err:
            set_task(status="error", stage="生成失败", error=err[:260])
            return
        set_task(status="done", stage="完成", **result)
    except Exception as e:
        set_task(status="error", stage="生成失败", error=f"{type(e).__name__}: {e}"[:260])


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive：手机端连续翻页/请求复用 TCP 连接，明显加快响应
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if getattr(self, "_head_only", False):
            # HEAD 请求：只回头部不回正文（前端用它探测原始 PDF 是否存在）
            return
        self.wfile.write(body)

    # 普通 JSON 接口仍限制为 1MB；/api/upload-pdf 走 file_data(base64) 时需更大上限，
    # 由 do_POST 在上传分支单独放宽。
    _JSON_MAX = 1024 * 1024

    def _read_json(self, max_bytes=None):
        """读取一个受限大小的 JSON 请求体；错误时返回 None。

        max_bytes 可覆盖默认上限（上传接口传 base64 时放宽到数十 MB）。
        """
        cap = int(max_bytes if max_bytes is not None else self._JSON_MAX)
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > cap:
            self._send(413, {"ok": False, "error": "请求体过大（上限 %dMB）" % (cap // (1024 * 1024))})
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            self._send(400, {"ok": False, "error": "bad json"})
            return None

    def _read_pdf_upload(self):
        """解析单文件 multipart 上传，且只落盘通过 PDF 文件头校验的内容。"""
        cfg = STATE["cfg"].get("uploads") or {}
        max_bytes = int(cfg.get("max_mb", 40)) * 1024 * 1024
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > max_bytes:
            self._send(413, {"ok": False, "error": f"PDF 大小需在 0-{cfg.get('max_mb', 40)}MB 内"})
            return None
        ctype = self.headers.get("Content-Type") or ""
        m = re.search(r"boundary=(?:\"([^\"]+)\"|([^;\s]+))", ctype, re.I)
        if "multipart/form-data" not in ctype.lower() or not m:
            self._send(400, {"ok": False, "error": "请用 multipart/form-data 上传 PDF"})
            return None
        boundary = (m.group(1) or m.group(2)).encode("utf-8")
        raw = self.rfile.read(n)
        file_name, pdf = "", None
        for part in raw.split(b"--" + boundary):
            if b"Content-Disposition:" not in part or b"\r\n\r\n" not in part:
                continue
            head, body = part.split(b"\r\n\r\n", 1)
            nm = re.search(br'filename="([^\"]*)"', head, re.I)
            if not nm:
                continue
            try:
                file_name = nm.group(1).decode("utf-8", "replace")
            except Exception:
                file_name = "uploaded.pdf"
            pdf = body.rstrip(b"\r\n")
            break
        if not pdf or not pdf.lstrip().startswith(b"%PDF-"):
            self._send(400, {"ok": False, "error": "文件不是有效 PDF（缺少 %PDF- 文件头）"})
            return None
        safe_title = os.path.basename(file_name).replace("\x00", "") or "uploaded.pdf"
        token = uuid.uuid4().hex
        target = os.path.join(STATE["upload_dir"], token + ".pdf")
        with open(target, "wb") as f:
            f.write(pdf)
        return {"id": token, "name": safe_title, "size": len(pdf), "created_at": int(time.time())}

    def _save_pdf_bytes(self, pdf, filename):
        """校验大小/PDF 文件头并落盘，返回 meta dict；不合法时已发送响应并返回 None。"""
        cfg = STATE["cfg"].get("uploads") or {}
        max_bytes = int(cfg.get("max_mb", 40)) * 1024 * 1024
        if len(pdf) > max_bytes:
            self._send(413, {"ok": False, "error": f"PDF 大小需在 0-{cfg.get('max_mb', 40)}MB 内"})
            return None
        if not pdf.lstrip().startswith(b"%PDF-"):
            self._send(400, {"ok": False, "error": "上传内容不是有效 PDF（缺少 %PDF- 文件头）"})
            return None
        safe_title = os.path.basename(str(filename or "uploaded.pdf").replace("\x00", "")) or "uploaded.pdf"
        token = uuid.uuid4().hex
        target = os.path.join(STATE["upload_dir"], token + ".pdf")
        with open(target, "wb") as f:
            f.write(pdf)
        return {"id": token, "name": safe_title, "size": len(pdf), "created_at": int(time.time())}

    def _read_pdf_upload_fileid(self, req):
        """callContainer 模式：PDF 经小程序上传后回传标识，后端落盘。

        来源（按优先级）：
        1. file_data：base64 直传——仅公网/自有域名模式可用
           （callContainer 官方限制请求体 ≤100KB，小程序端请走 /api/upload-pdf-chunk 分块）
        2. file_url：小程序 getTempFileURL 换出的临时直链，后端下载（免 WX）
        3. fileID：云存储 fileID，后端 batchdownloadfile 取回
           （凭证优先云托管自动令牌/开放接口服务，环境 ID 可从 fileID 自动解析）
        """
        cfg = STATE["cfg"].get("uploads") or {}
        max_bytes = int(cfg.get("max_mb", 40)) * 1024 * 1024
        file_data = (req.get("file_data") or "").strip()
        file_url = (req.get("file_url") or "").strip()
        file_id = (req.get("fileID") or "").strip()

        # 1) 直传 base64（最稳，免密钥）
        if file_data:
            try:
                pdf = base64.b64decode(file_data)
            except Exception as e:
                self._send(400, {"ok": False, "error": f"file_data 不是合法 base64：{e}"[:220]})
                return None
        # 2) 临时直链下载（免 WX）
        elif file_url:
            try:
                req_dl = urllib.request.Request(
                    file_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; LiteratureLens/1.0)",
                             "Referer": "https://servicewechat.com/"},
                )
                with urllib.request.urlopen(req_dl, timeout=60) as r:
                    pdf = r.read()
            except Exception as e:
                self._send(200, {"ok": False, "error": f"从云存储下载 PDF 失败：{e}"[:220]})
                return None
        # 3) fileID 回退（凭证走云托管令牌/开放接口服务/AppSecret）
        elif file_id:
            try:
                dl = _wx_file_download_url(file_id)
            except RuntimeError as e:
                self._send(200, {"ok": False, "error": str(e)[:220]})
                return None
            try:
                req_dl = urllib.request.Request(
                    dl,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; LiteratureLens/1.0)",
                             "Referer": "https://servicewechat.com/"},
                )
                with urllib.request.urlopen(req_dl, timeout=30) as r:
                    pdf = r.read()
            except Exception as e:
                self._send(200, {"ok": False, "error": f"从云存储下载 PDF 失败：{e}"[:220]})
                return None
        else:
            self._send(400, {"ok": False, "error": "缺少 file_data / file_url / fileID（PDF 上传后回传）"})
            return None

        return self._save_pdf_bytes(pdf, req.get("filename"))

    # 分块直传缓冲：upload_id → 临时 base64 文件放系统临时目录，最后一块组装落盘
    _CHUNK_ID_RE = re.compile(r"^[a-f0-9]{8,64}$")

    def _read_pdf_chunk(self, req):
        """callContainer 请求体 ≤100KB 的终极兜底：小程序把 PDF 切成 base64 块逐个直传，
        最后一块组装落盘。完全不依赖云存储 / WX 密钥 / 对象存储签名。

        请求：{upload_id, seq, total, data(base64片段), filename}
              {upload_id, abort:true} 中止并清理
        """
        upload_id = (req.get("upload_id") or "").strip()
        if not self._CHUNK_ID_RE.match(upload_id):
            self._send(400, {"ok": False, "error": "upload_id 不合法（需 8-64 位十六进制）"})
            return
        tmp = os.path.join(tempfile.gettempdir(), "lens_chunk_" + upload_id + ".b64")
        if req.get("abort"):
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            self._send(200, {"ok": True, "aborted": True})
            return
        cfg = STATE["cfg"].get("uploads") or {}
        max_bytes = int(cfg.get("max_mb", 40)) * 1024 * 1024
        try:
            seq = int(req.get("seq") or 0)
            total = int(req.get("total") or 0)
        except Exception:
            seq = total = 0
        data = re.sub(r"\s+", "", str(req.get("data") or ""))
        if total <= 0 or seq < 0 or seq >= total or not data:
            self._send(400, {"ok": False, "error": "分块参数不合法（seq/total/data）"})
            return
        # 容量闸门：base64 膨胀约 4/3，累计上限 = max_bytes*4/3 + 余量
        cur = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        if cur + len(data) > int(max_bytes * 4 / 3) + 65536:
            try:
                os.remove(tmp)
            except Exception:
                pass
            self._send(413, {"ok": False, "error": f"PDF 大小需在 0-{cfg.get('max_mb', 40)}MB 内"})
            return
        try:
            with open(tmp, "ab") as f:
                f.write(data.encode("ascii"))
        except Exception as e:
            self._send(500, {"ok": False, "error": f"分块写入失败：{e}"[:200]})
            return
        if seq < total - 1:
            self._send(200, {"ok": True, "received": seq + 1, "total": total})
            return
        # 最后一块：读取 → 组装 → 落盘（临时文件无论成败都删除）
        try:
            with open(tmp, "rb") as f:
                b64 = f.read().decode("ascii")
        except Exception as e:
            self._send(500, {"ok": False, "error": f"分块读取失败：{e}"[:200]})
            return
        try:
            os.remove(tmp)
        except Exception:
            pass
        try:
            pdf = base64.b64decode(b64)
        except Exception as e:
            self._send(400, {"ok": False, "error": f"base64 组装失败：{e}"[:200]})
            return
        meta = self._save_pdf_bytes(pdf, req.get("filename"))
        if meta:
            meta["url"] = "/uploads/" + meta["id"] + ".pdf"
            self._send(200, {"ok": True, "upload": meta})

    def _llm_required(self):
        """未配置任何 AI 模型时，回 200 ok:False 并 return True（已处理）。

        云端「各端自行配置 Key」场景下，后端仍需正常提供 PDF 存储 / 页图 /
        文献列表（这些不依赖 LLM）；只有翻译 / 对话 / 导图 / 分析等需要模型的
        接口，在这里被友好拦截，避免 500。
        """
        if not STATE["llm"] or not getattr(STATE["llm"], "providers", None):
            self._send(200, {"ok": False, "error": (
                "后端未配置 AI 模型。请在本机网页「设置 → API」粘贴你的 Key "
                "并点测试连接；或在小程序「我的」里自行配置各端 API。")})
            return True
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_HEAD(self):
        """前端 renderOrigPdf 用 HEAD 探测原始 PDF 是否存在；
        http.server 默认对 HEAD 返回 501，导致永远走页图回退。"""
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html", "/lens"):
            # 每次请求都重读网页，改完 template + render 后刷新即可看到
            if STATE["html_path"] and os.path.exists(STATE["html_path"]):
                try:
                    STATE["html"] = open(STATE["html_path"], encoding="utf-8").read()
                except Exception:
                    pass
            if STATE["html"]:
                self._send(200, STATE["html"], "text/html; charset=utf-8")
            else:
                self._send(404, "网页未生成", "text/plain; charset=utf-8")
        elif p in ("/api/health", "/health"):
            llm = STATE["llm"]
            act = {}
            try:
                if llm:
                    act = llm.provider()
            except Exception:
                act = {}
            self._send(200, {
                "ok": True,
                "model": act.get("name", ""),
                "provider": act.get("id", ""),
                "protocol": _norm_protocol(act) if act else "",
                "n_docs": len(STATE["doc_index"]),
                "providers": llm.list_providers() if llm else [],
            })
        elif p == "/api/models":
            self._send(200, {"ok": True,
                             "active": STATE["llm"].active if STATE["llm"] else "",
                             "providers": STATE["llm"].list_providers() if STATE["llm"] else []})
        elif p == "/api/settings":
            # 只返回可展示的配置,绝不把 API key 回传到浏览器。
            ps = STATE["llm"].list_providers() if STATE["llm"] else []
            act_id = STATE["llm"].active if STATE["llm"] else ""
            active = next((p for p in ps if p["id"] == act_id), None)
            self._send(200, {
                "ok": True,
                "active": act_id,
                "providers": ps,
                "has_user_config": os.path.isfile(os.path.join(os.path.dirname(HERE), "config.user.json")),
                "active_summary": {
                    "id": active["id"], "name": active["name"],
                    "model": active["model"], "protocol": active["protocol"]
                } if active else None,
                "presets": API_PRESETS,
                "egress": STATE["cfg"].get("egress") or {},
            })
        elif p == "/api/analyze-pdf":
            # 前端轮询解析进度:?task=<task_id>
            task = urlparse(self.path).query
            m = re.search(r"(?:^|&)task=([\w-]+)", task)
            tid = m.group(1) if m else ""
            with _TASKS_LOCK:
                t = dict(TASKS.get(tid) or {})
            if not t:
                self._send(404, {"ok": False, "error": "未知解析任务"})
                return
            self._send(200, {"ok": True, **t})
        elif p == "/api/task":
            # 通用任务轮询：上传解析 / 大模型请求共用同一 TASKS 字典。
            task = urlparse(self.path).query
            m = re.search(r"(?:^|&)task=([\w-]+)", task)
            tid = m.group(1) if m else ""
            with _TASKS_LOCK:
                t = dict(TASKS.get(tid) or {})
            if not t:
                self._send(404, {"ok": False, "error": "未知任务"})
                return
            self._send(200, {"ok": True, **t})
        elif p == "/api/docs":
            # 小程序端文献列表：只返回轻量 meta（不含译文正文，控制流量）
            docs = []
            for d in (STATE["data"].get("docs") or []):
                meta = d.get("meta") or {}
                stats = d.get("stats") or {}
                docs.append({
                    "id": d.get("id", ""),
                    "title": meta.get("title") or "(未命名)",
                    "title_zh": meta.get("title_zh") or "",
                    "lang_src": meta.get("lang_src") or "en",
                    "source_key": meta.get("source_key") or "",
                    "year": meta.get("year") or "",
                    "n_pages": stats.get("n_pages") or 0,
                    "n_words": stats.get("n_words") or 0,
                })
            self._send(200, {"ok": True, "docs": docs})
        elif p == "/api/doc":
            # 小程序端整篇文献数据（画像 / 双语译文块 / 创新点等）
            q = urlparse(self.path).query
            m = re.search(r"(?:^|&)doc=([\w-]+)", q)
            did = m.group(1) if m else ""
            doc = STATE["doc_index"].get(did)
            if not doc:
                self._send(404, {"ok": False, "error": "not found"})
                return
            d2 = {k: v for k, v in doc.items() if k != "pages_img"}   # 页图走 /api/page-image，不内联
            self._send(200, {"ok": True, "doc": d2})
        elif p.startswith("/data/pages/") and p.endswith(".jpg"):
            # 上传解析文献的原页图（只允许 <docid>/pN.jpg 形式，杜绝路径穿越）
            m = re.fullmatch(r"/data/pages/([0-9a-f]{8})/p(\d{1,3})\.jpg", p)
            if not m:
                self._send(404, {"ok": False, "error": "not found"})
                return
            # 与 _page_image 的落盘位置保持一致：data_path 同级的 pages/ 目录
            # （云端 data_path=/data/lens_data.json → 这里取 /data/pages）。
            pages_root = os.path.join(os.path.dirname(STATE["data_path"]), "pages")
            fp = os.path.join(pages_root, m.group(1), f"p{m.group(2)}.jpg")
            if not os.path.isfile(fp):
                self._send(404, {"ok": False, "error": "not found"})
                return
            with open(fp, "rb") as f:
                # 内容不变（doc id 为内容 hash，页图落盘后不再改）→ 让客户端长缓存
                self._send(200, f.read(), "image/jpeg",
                           {"Cache-Control": "public, max-age=604800"})
        elif p == "/api/page-image":
            # 按需渲染缺页页图：上传文献预渲染页图不足时实时补齐（落盘缓存）
            q = urlparse(self.path).query
            m = re.search(r"(?:^|&)doc=([\w-]+)", q)
            did = m.group(1) if m else ""
            mp = re.search(r"(?:^|&)page=(\d+)", q)
            page_no = int(mp.group(1)) if mp else 0
            doc = STATE["doc_index"].get(did)
            if not doc or page_no < 1:
                self._send(404, {"ok": False, "error": "not found"})
                return
            fp = _page_image(did, doc, page_no)
            if not fp:
                self._send(404, {"ok": False, "error": "该页无法渲染"})
                return
            # 后台预渲染后两页：翻页时命中落盘缓存，秒开
            threading.Thread(
                target=lambda: [_page_image(did, doc, page_no + k) for k in (1, 2)],
                daemon=True).start()
            with open(fp, "rb") as f:
                self._send(200, f.read(), "image/jpeg",
                           {"Cache-Control": "public, max-age=604800"})
        elif p == "/api/page-words":
            # 页图划选文字层的坐标数据：fitz 按页提取单词框（归一化到 0-1000），
            # 供前端铺透明文字层 → 划选即为真实 DOM 选区 → 划选翻译可用。
            q = urlparse(self.path).query
            m = re.search(r"(?:^|&)doc=([\w-]+)", q)
            did = m.group(1) if m else ""
            mp = re.search(r"(?:^|&)page=(\d+)", q)
            page_no = int(mp.group(1)) if mp else 0
            doc = STATE["doc_index"].get(did)
            if not doc or page_no < 1:
                self._send(404, {"ok": False, "error": "not found"})
                return
            words = _words_cache_get(did, doc, page_no)
            if words is None:
                self._send(200, {"ok": False, "error": "该页没有可提取的文字坐标"})
                return
            self._send(200, {"ok": True, "page": page_no, "words": words})
        elif p == "/api/source-pdf":
            # 内置 PDF 阅读器数据源：返回该文献的原始 PDF（本地上传的文献）
            q = urlparse(self.path).query
            m = re.search(r"(?:^|&)doc=([\w-]+)", q)
            did = m.group(1) if m else ""
            doc = STATE["doc_index"].get(did)
            if not doc or (doc.get("meta") or {}).get("source_key") != "upload":
                self._send(404, {"ok": False, "error": "not found"})
                return
            fp = _resolve_upload_pdf(doc)
            if not fp:
                self._send(404, {"ok": False, "error": "找不到对应的原始 PDF，请重新上传"})
                return
            with open(fp, "rb") as f:
                self._send(200, f.read(), "application/pdf")
        elif p.startswith("/uploads/") and p.endswith(".pdf"):
            # 只允许 UUID 命名的本服务上传文件，杜绝路径穿越和任意文件读取。
            token = os.path.basename(p)[:-4]
            if not re.fullmatch(r"[0-9a-f]{32}", token):
                self._send(404, {"ok": False, "error": "not found"})
                return
            fp = os.path.join(STATE["upload_dir"], token + ".pdf")
            if not os.path.isfile(fp):
                self._send(404, {"ok": False, "error": "上传文件不存在或已被清理"})
                return
            with open(fp, "rb") as f:
                self._send(200, f.read(), "application/pdf")
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/test":
            # 用临时表单里的配置做一次连通性+JSON 能力探测,绝不落盘。
            # 既支持"测已存 provider",也支持"测用户即将填的新 API"。
            req = self._read_json()
            if req is None:
                return
            t0 = time.time()
            try:
                if req.get("pid"):
                    # 测已存 provider:直接复用 STATE 里现成的
                    out = STATE["llm"].probe(pid=req.get("pid"), timeout=int(req.get("timeout") or 45))
                else:
                    # 测临时配置:临时构造一个不进入 STATE 的 CloudLLM
                    tmp = _build_temporary_llm(req)
                    out = tmp.probe(timeout=int(req.get("timeout") or 45))
            except Exception as e:
                self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"[:220]})
                return
            out["latency_ms"] = int((time.time() - t0) * 1000)
            self._send(200, out)
            return
        if p == "/api/chat":
            # AI 助手多轮对话:锚定当前文献,按"翻译/analyze"角色调用。
            req = self._read_json()
            if req is None:
                return
            doc = STATE["doc_index"].get(req.get("doc_id"))
            if not doc:
                self._send(404, {"ok": False, "error": "请先从左侧选择一篇文献"})
                return
            messages = req.get("messages") or []
            if not isinstance(messages, list) or not messages:
                self._send(400, {"ok": False, "error": "messages 不能为空"})
                return
            # 拼接上下文:仅取最近 8 条,过长截断;每条 2000 字封顶
            trimmed = []
            for m in messages[-8:]:
                if not isinstance(m, dict):
                    continue
                role = (m.get("role") or "").strip().lower()
                if role not in ("user", "assistant", "system"):
                    continue
                content = str(m.get("content") or "").strip()[:2000]
                if content:
                    trimmed.append({"role": role, "content": content})
            if not trimmed:
                self._send(400, {"ok": False, "error": "没有有效消息"})
                return
            # 省 Token 关键:只取与最后一条用户提问相关的段落(而非全文前 60 段)
            last_user = ""
            for m in reversed(trimmed):
                if m["role"] == "user":
                    last_user = m["content"]
                    break
            ctx = _relevant_ctx(doc, last_user or (trimmed[-1]["content"] if trimmed else ""))
            profile = doc.get("profile") or {}
            profile_text = json.dumps({
                "title_zh": profile.get("title_zh", ""),
                "research_question": profile.get("research_question", ""),
                "method": profile.get("method", ""),
                "findings": profile.get("findings", []),
                "keywords": profile.get("keywords", []),
            }, ensure_ascii=False)
            # 登录时填写的「研究方向」,让 AI 在该专业语境下作答
            field = (req.get("field") or "").strip()
            field_line = (
                f"\n【提问者研究方向】{field}\n"
                "对方可能是该领域的科研工作者,请用其熟悉的专业语言作答。"
                if field else "")
            # 阅读指针:用户在内置 PDF 阅读器里正在看的页码,回答优先结合该页
            ptr = req.get("pointer") or {}
            ptr_line = ""
            if isinstance(ptr, dict) and ptr.get("page"):
                ptr_line = (f"\n【阅读指针】用户正在阅读原文第 {ptr.get('page')} 页,"
                            "回答时优先结合该页的内容与图表。")
            # 用 chat_text (走 chat 角色) 而非 chat_json,允许多轮自然对话
            ctx_block = (
                "【文献标题】" + (doc["meta"].get("title") or "") + "\n"
                "【文献画像】" + profile_text[:1500] + "\n"
                "【正文片段（已按问题相关性筛选）】\n"
                + ctx[:10000] + field_line + ptr_line
            )
            try:
                # 把历史消息作为前缀;系统约束放在最前
                system_prompt = (
                    "你是文献透镜的 AI 科研助手,擅长根据当前论文回答用户的问题。"
                    "回答必须基于以下文献语境;无法确认时明确写「原文未提及」,绝不编造。"
                    "回复简洁有层次,优先用中文,长度控制在 600 字以内。\n\n" + ctx_block
                )
                # OpenAI 兼容 chat 接口需要 messages 数组;直接复用 CloudLLM.chat
                history = [{"role": "system", "content": system_prompt}] + trimmed
                reply = STATE["llm"].chat(
                    history[-1]["content"],
                    system=system_prompt,
                    temperature=0.25,
                    max_tokens=1500,
                )
            except LLMError as e:
                self._send(200, {"ok": False, "error": f"{e}"[:220]})
                return
            except Exception as e:
                self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
                return
            if not reply:
                self._send(200, {"ok": False, "error": "模型未返回有效内容"})
                return
            pinfo = STATE["llm"].provider()
            self._send(200, {
                "ok": True,
                "reply": str(reply).strip(),
                "model": pinfo.get("name", ""),
                "usage_est": {
                    "in_chars": sum(len(m["content"]) for m in trimmed) + len(system_prompt),
                    "out_chars": len(reply.strip()),
                }
            })
            return
        if p == "/api/upload-pdf":
            # 方式一（本地 / 自有域名）：multipart 直传。
            # 方式二（callContainer）：JSON 带 fileID，后端从云存储取回。
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "multipart/form-data" in ctype:
                meta = self._read_pdf_upload()
            else:
                # file_url / file_data(base64) 直传：放宽 JSON 上限到 30MB
                req = self._read_json(max_bytes=30 * 1024 * 1024)
                if req is None:
                    return
                meta = self._read_pdf_upload_fileid(req)
            if meta:
                meta["url"] = "/uploads/" + meta["id"] + ".pdf"
                self._send(200, {"ok": True, "upload": meta})
            return
        if p == "/api/upload-pdf-chunk":
            # callContainer 请求体 ≤100KB 的分块直传：小程序把 PDF 切成 base64 块
            # 逐个发送，最后一块组装落盘。零依赖（不碰云存储/WX 密钥），兜底最稳。
            req = self._read_json(max_bytes=256 * 1024)
            if req is None:
                return
            self._read_pdf_chunk(req)
            return
        if p == "/api/llm-task":
            # callContainer 异步化入口：大模型请求立即返回 task_id，后台线程跑，
            # 前端轮询 /api/task 取结果（规避 callContainer ≤15s 超时）。
            # 同步（uni.request）模式同样可用，返回结构一致。
            req = self._read_json()
            if req is None:
                return
            action = (req.get("action") or "").strip().lower()
            if action not in _LLM_STAGE:
                self._send(400, {"ok": False, "error": f"未知的 AI 动作：{action}"})
                return
            task_id = uuid.uuid4().hex
            _set_task(task_id, status="queued", stage=_LLM_STAGE.get(action, "排队中…"),
                      action=action, created_at=int(time.time()))
            threading.Thread(target=_llm_worker, args=(task_id, action, req.get("payload") or {}),
                             daemon=True).start()
            self._send(200, {"ok": True, "task": task_id})
            return
        if p == "/api/analyze-pdf":
            # 启动后台解析线程：上传 PDF → 抽取/翻译/创新点分析 → 写入文献库
            req = self._read_json()
            if req is None:
                return
            upload_id = (req.get("upload_id") or "").strip()
            filename = (req.get("filename") or "").strip()
            if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
                self._send(400, {"ok": False, "error": "无效的 upload_id"})
                return
            pdf_path = os.path.join(STATE["upload_dir"], upload_id + ".pdf")
            if not os.path.isfile(pdf_path):
                self._send(404, {"ok": False, "error": "上传文件已过期或不存在，请重新上传"})
                return
            task_id = uuid.uuid4().hex
            _set_task(task_id, status="queued", stage="排队中…",
                      upload_id=upload_id, filename=filename,
                      created_at=int(time.time()))
            # 允许前端（手机）随请求带来临时 LLM 配置（用手机已配的 Key），后端无需任何 AI 配置
            llm_cfg = req.get("llm") or None
            threading.Thread(
                target=_analyze_worker,
                args=(task_id, upload_id, filename, llm_cfg),
                daemon=True,
            ).start()
            self._send(200, {"ok": True, "task": task_id})
            return
        if p == "/api/reanalyze":
            # 重新解析已有文献：重跑 翻译 + 速览/创新点（Key 失效后补救 / 拖拽触发分析）
            if self._llm_required():
                return
            req = self._read_json()
            if req is None:
                return
            did = (req.get("doc") or "").strip()
            if did not in STATE["doc_index"]:
                self._send(404, {"ok": False, "error": "文献不存在"})
                return
            task_id = uuid.uuid4().hex
            _set_task(task_id, status="queued", stage="排队中…", doc_id=did,
                      created_at=int(time.time()))
            threading.Thread(target=_reanalyze_worker, args=(task_id, did),
                             daemon=True).start()
            self._send(200, {"ok": True, "task": task_id})
            return
        if p == "/api/settings":
            # 页面可录入用户自己的 API:支持三种载荷
            #   - {single: {...}}                        单 API 全角色(推荐,90% 用户)
            #   - {preset: "deepseek", api_key: "..."}   预设 + 只粘 Key
            #   - {translation: {...}, reasoning: {...}} 双 API 模式(沿用兼容)
            # key 只写入本机 config.user.json,不出现在响应体/日志/网页。
            req = self._read_json()
            if req is None:
                return
            try:
                payload = _normalize_settings_payload(req)
                providers = payload["providers"]
                active = payload["active"]
            except ValueError as e:
                self._send(400, {"ok": False, "error": str(e)[:200]})
                return
            except Exception as e:
                self._send(400, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
                return
            save_user_config({"active_provider": active, "providers": providers})
            try:
                load(STATE["data_path"], STATE["html_path"],
                     load_config(require_provider=False))
            except Exception as e:
                self._send(500, {"ok": False, "error": f"配置保存了,但重新载入失败:{e}"[:220]})
                return
            self._send(200, {
                "ok": True,
                "active": active,
                "providers": STATE["llm"].list_providers(),
                "message": payload.get("message", "用户 API 已启用并热重载。")
            })
            return

        if p == "/api/egress":
            # 统一出网配置：保存代理 + 检索提供方 + 置信度开关，并热应用。
            req = self._read_json()
            if req is None:
                return
            eg = req.get("egress")
            if not isinstance(eg, dict):
                self._send(400, {"ok": False, "error": "egress 必须是对象"})
                return
            # 规整 proxy 子对象
            prox = eg.get("proxy") or {}
            if not isinstance(prox, dict):
                prox = {}
            eg = dict(eg)
            eg["proxy"] = {k: str(prox.get(k) or "").strip()
                           for k in ("http", "https", "no_proxy")}
            try:
                save_user_config({"egress": eg})
                applied = apply_proxy(eg)
                STATE["cfg"]["egress"] = eg
                self._send(200, {
                    "ok": True,
                    "applied": applied,
                    "egress": eg,
                    "message": "出网配置已保存并热应用（代理%s生效）。"
                               % ("已" if (applied.get("HTTPS_PROXY") or applied.get("HTTP_PROXY")) else "未设，直连"),
                })
            except Exception as e:
                self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if p == "/api/egress-test":
            # 优先用「表单当前值」测试（不落盘），否则用已保存配置。
            # 先预检代理端口，再做一次学术网页检索，验证能出网 + 看结果数。
            req = self._read_json() or {}
            eg = req.get("egress") if isinstance(req.get("egress"), dict) \
                else (STATE["cfg"].get("egress") or {})
            q = (req.get("query") or "concrete compressive strength").strip()
            n = int(req.get("n") or 6)
            # 代理预检：填了代理但端口没监听时，给出最直接的提示
            px = ((eg.get("proxy") or {}).get("https")
                  or (eg.get("proxy") or {}).get("http") or "").strip()
            if px:
                m = re.match(r"^(?:https?://)?([^:/@]+@)?([\w.\-]+):(\d+)", px)
                if m:
                    host, port = m.group(2), int(m.group(3))
                    try:
                        import socket as _s
                        with _s.create_connection((host, port), timeout=2):
                            pass
                    except Exception as pe:
                        self._send(200, {
                            "ok": False,
                            "error": f"代理 {host}:{port} 连不上（{type(pe).__name__}）"
                                     "——代理软件没开？可清空代理直连，或开启代理后再试",
                            "egress": eg})
                        return
            try:
                res = web_search(q, n=n, egress=eg)
                self._send(200, {
                    "ok": True,
                    "provider": eg.get("search_provider", "bing"),
                    "note": getattr(web_search, "last_note", ""),
                    "count": len(res),
                    "sample": [{"title": (r.get("title") or "")[:80],
                                "url": r.get("url"), "academic": bool(r.get("academic"))}
                               for r in res[:6]],
                    "egress": eg,
                })
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)[:220],
                                "egress": eg})
            return

        if p == "/api/use":
            req = self._read_json()
            if req is None:
                return
            pid = req.get("provider")
            ids = [x.get("id") for x in (STATE["cfg"].get("providers") or [])]
            if pid not in ids:
                self._send(400, {"ok": False, "error": f"未知 provider：{pid}",
                                 "available": ids})
                return
            STATE["llm"].active = pid
            if STATE["az"]:
                STATE["az"].pid = None      # 交回角色路由
            self._send(200, {"ok": True, "active": pid,
                             "model": STATE["llm"].provider()["name"]})
            return

        if p == "/api/translate":
            # 划词翻译的在线兜底:本地已译内容匹配不到时才走到这里。
            # 先看服务端翻译缓存(落盘),命中直接返回 —— 同一句只向 API 付一次 Token。
            if self._llm_required():
                return
            req = self._read_json()
            if req is None:
                return
            text = (req.get("text") or "").strip()
            if not text:
                self._send(400, {"ok": False, "error": "text 为空"})
                return
            text = text[:4000]
            mode = (req.get("mode") or "general").lower()
            want_conf = bool(req.get("with_confidence")) or \
                bool((STATE["cfg"].get("egress") or {}).get("translate_confidence"))
            translation, cached, err = _translate_text(text, mode)
            if err:
                self._send(200, {"ok": False, "error": err})
                return
            # 学术置信度（best-effort）：命中请求开关或全局配置才算，绝不阻断翻译。
            confidence = None
            if want_conf:
                try:
                    confidence = translation_confidence(
                        text, translation,
                        STATE["cfg"].get("egress"),
                        timeout=(STATE["cfg"].get("egress") or {}).get("search_timeout", 10))
                except Exception:
                    confidence = None
            self._send(200, {"ok": True, "translation": translation,
                             "mode": mode, "cached": cached,
                             "model": STATE["llm"].provider()["name"],
                             "confidence": confidence})
            return

        if p == "/api/translate-page":
            # 定向翻译：前端「阅读指针」指定当前 PDF 的某一页，
            # 由后端定位该文献的原始 PDF → 提取该页原文 → 整页翻译（带缓存）。
            if self._llm_required():
                return
            req = self._read_json()
            if req is None:
                return
            doc_id = str(req.get("doc") or "")
            try:
                page_no = int(req.get("page") or 0)
            except (TypeError, ValueError):
                page_no = 0
            doc = STATE["doc_index"].get(doc_id)
            if doc is None or page_no < 1:
                self._send(400, {"ok": False, "error": "doc 或 page 参数无效"})
                return
            text = ""
            if (doc.get("meta") or {}).get("source_key") == "upload":
                pdf = _resolve_upload_pdf(doc)
                if pdf:
                    text = _page_text_from_pdf(pdf, page_no)
            if not text:
                pt = doc.get("pages_txt") or {}
                text = pt.get(str(page_no)) or pt.get(page_no) or ""
            text = (text or "").strip()
            if not text:
                self._send(200, {"ok": False,
                                 "error": f"第 {page_no} 页没有可提取的文本（扫描版 PDF 或超出页码范围）"})
                return
            chars = len(text)
            text = text[:4000]     # 与划选翻译同一上限，保护 Token
            want_conf = bool(req.get("with_confidence"))
            translation, cached, err = _translate_text(text, "academic")
            if err:
                self._send(200, {"ok": False, "error": err})
                return
            confidence = None
            if want_conf:
                try:
                    confidence = translation_confidence(
                        text, translation,
                        STATE["cfg"].get("egress"),
                        timeout=(STATE["cfg"].get("egress") or {}).get("search_timeout", 10))
                except Exception:
                    confidence = None
            self._send(200, {"ok": True, "translation": translation,
                             "page": page_no, "chars": chars,
                             "cached": cached,
                             "model": STATE["llm"].provider()["name"],
                             "confidence": confidence})
            return

        if p == "/api/mindmap":
            if self._llm_required():
                return
            req = self._read_json()
            if req is None:
                return
            question = (req.get("question") or "").strip()
            doc = STATE["doc_index"].get(req.get("doc_id"))
            if not question:
                self._send(400, {"ok": False, "error": "请先输入想理解的问题"})
                return
            if not doc:
                self._send(404, {"ok": False, "error": "请先从左侧选择一篇文献"})
                return
            # 省 Token:按问题相关性取相关段落,而非前 80 段全塞
            ctx = _relevant_ctx(doc, question)
            result = STATE["az"].question_map(question, ctx, doc.get("profile") or {})
            if not result:
                self._send(200, {"ok": False, "error": "导图生成失败；请检查用户提供的推理 API 或稍后重试"})
                return
            pinfo = STATE["llm"].pick_for("mindmap") or STATE["llm"].provider()
            self._send(200, {"ok": True, "mindmap": result,
                             "model": pinfo.get("name") or pinfo.get("id", "")})
            return

        if p != "/api/deepen":
            self._send(404, {"ok": False, "error": "not found"})
            return
        req = self._read_json()
        if req is None:
            return
        if self._llm_required():
            return

        doc = STATE["doc_index"].get(req.get("doc_id"))
        cid = req.get("concept_id")
        if not doc:
            self._send(404, {"ok": False, "error": "文献不存在"})
            return
        concept = next((c for c in doc.get("concepts", []) if c["id"] == cid), None)
        if not concept:
            self._send(404, {"ok": False, "error": "知识点不存在"})
            return

        ctx = "\n\n".join(b["source"] for b in doc.get("bilingual", [])[:40])
        try:
            d = STATE["az"].deepen(concept, ctx)
        except LLMError as e:
            self._send(200, {"ok": False, "error": f"{e}"[:220]})
            return
        except Exception as e:
            self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return
        if not d:
            self._send(200, {"ok": False, "error": "模型未返回有效内容"})
            return
        concept["detail"] = d
        self._send(200, {"ok": True, "detail": d, "model": STATE["llm"].provider()["name"]})


def main():
    ap = argparse.ArgumentParser()
    # 容器 / 云部署：关键参数优先取环境变量（CloudBase Run 注入 PORT；数据目录挂卷到 /data）
    _dflt_data = os.environ.get("LENS_DATA") or os.path.join(os.path.dirname(HERE), "data", "lens_data.json")
    _dflt_html = os.environ.get("LENS_HTML") or os.path.join(os.path.dirname(HERE), "outputs", "literature_lens.html")
    _dflt_port = int(os.environ.get("PORT") or 8877)
    _dflt_host = os.environ.get("HOST", "0.0.0.0")
    ap.add_argument("--data", default=_dflt_data)
    ap.add_argument("--html", default=_dflt_html)
    ap.add_argument("--port", type=int, default=_dflt_port)
    ap.add_argument("--host", default=_dflt_host,
                    help="默认 0.0.0.0：允许手机真机经局域网/公网访问")
    ap.add_argument("--provider", default=None, help="临时指定用于深度讲解的模型")
    ap.add_argument("--config", default=None)
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.data):
        # 容器首次运行：持久卷里尚无数据文件 → 初始化空库，避免直接退出
        try:
            os.makedirs(os.path.dirname(a.data) or ".", exist_ok=True)
            with open(a.data, "w", encoding="utf-8") as f:
                json.dump({"docs": []}, f, ensure_ascii=False)
            print("已初始化空文献库：", a.data)
        except Exception as e:
            print("找不到/无法创建数据文件：", a.data, e)
            sys.exit(1)
    cfg = load_config(extra=a.config, require_provider=False)
    if a.provider:
        cfg["active_provider"] = a.provider
    load(a.data, a.html, cfg)
    # 接管用户访问互联网的地址：把代理写进进程级环境变量，
    # 全服务（翻译 API + 学术检索）统一经此出口出网。
    apply_proxy(cfg.get("egress"))
    if a.provider:
        STATE["az"].pid = a.provider

    llm = STATE["llm"]
    url = f"http://{a.host}:{a.port}/"
    print("=" * 60)
    print("  文献透镜 已启动")
    print(f"  地址：{url}")
    try:
        _prov = llm.provider()
        _name = _prov.get("name", "")
        _pid = _prov.get("id", "")
    except Exception:
        _name, _pid = "(未配置 AI 模型)", ""
    print(f"  分析模型：{_name}  (provider={_pid})")
    print("  可用模型：")
    try:
        _cur_id = llm.provider().get("id")
    except Exception:
        _cur_id = ""
    for p in llm.list_providers():
        flag = "  ← 当前" if p["id"] == _cur_id else ""
        off = " [禁用]" if not p["enabled"] else ""
        print(f"     - {p['id']:<12} {p['name']:<22} {p['model']}{off}{flag}")
    print(f"  文献：{len(STATE['doc_index'])} 篇")
    print("  按 Ctrl+C 停止")
    print("=" * 60)
    if a.open:
        webbrowser.open(url)
    # 多线程：页图渲染/大文件传输不再阻塞其他 API（原单线程 HTTPServer 会被
    # fitz 渲染卡住数秒，是小程序页图「加载中…」过慢的主因之一）
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
