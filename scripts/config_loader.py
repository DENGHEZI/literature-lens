# -*- coding: utf-8 -*-
"""
配置加载 —— 支持多文件叠加，换模型不改代码

配置查找顺序（后者覆盖前者）：
  1. <skill>/config.json                      随包默认
  2. <skill>/config.user.json                 用户本地覆盖（不随包分发，放自己的 key）
  3. --config /path/a.json[,/path/b.json]     命令行显式指定，最优先
  4. 环境变量 LITERATURE_LENS_CONFIG          指向额外配置文件

config.json 里所有 api_key 字段都支持「环境变量引用」写法：
    "api_key": "${DEEPSEEK_API_KEY}"
这样可以把 key 放在系统环境变量里，仓库里不落明文。

provider 最少只需 4 个字段：
    {"id": "...", "base_url": "...", "api_key": "...", "model": "..."}
其余（protocol / roles / ctx / max_output / supports_json）都可省略，有默认。
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ENV_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULTS = {
    "active_provider": "",
    "providers": [],
    "roles": {},
    "sources": {},
    "output": {"dir": os.path.join(ROOT, "outputs"),
               "site_name": "文献透镜 LiteratureLens"},
    "translate": {"max_chars_per_block": 2600, "keep_bilingual": True},
    "limits": {"max_pages": 8, "max_chars_body": 14000,
               "max_tokens_analyze": 7000, "max_tokens_terms": 1800},
    # 原页图片：整页渲染成 JPG 内进网页，保留图表/公式排版（体积约 110-130KB/页）
    # words=True 时顺带提取词级坐标（千分比，约 20-40KB/页），供原文分屏划词翻译
    "pages_images": {"enabled": True, "zoom": 1.25, "quality": 58, "words": True},
    # 网页端 PDF 上传：只在本机服务目录中保存，交给浏览器原生解释器显示。
    "uploads": {"dir": os.path.join(ROOT, "uploads"), "max_mb": 40},
    "fallback": {"on_error_switch_provider": True, "max_provider_tries": 2},
    # 统一出网（代理接管）+ 通用学术网页检索 + 翻译置信度
    # proxy 留空 = 直连；填了则全服务（翻译 API、学术检索）统一走代理出口。
    "egress": {
        "proxy": {"http": "", "https": "", "no_proxy": ""},
        "search_provider": "bing",         # bing(国内免代理可用) / duckduckgo / google / custom
        "search_url": "",                  # 仅 provider=custom 时需要：含 {q} 与 {n}
        "search_timeout": 10,              # 单次检索超时（秒）
        "translate_confidence": True,      # 翻译时是否附带学术置信度
    },
}


def _expand_env(v):
    if isinstance(v, str):
        return _ENV_RX.sub(lambda m: os.environ.get(m.group(1), ""), v)
    if isinstance(v, dict):
        return {k: _expand_env(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand_env(x) for x in v]
    return v


def _merge(base, top):
    """深合并：dict 递归，list 整体替换（providers 需要整段替换而非拼接）。"""
    out = dict(base)
    for k, v in (top or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _candidate_paths(extra=None, skip_global_user=False):
    paths = []
    env = os.environ.get("LITERATURE_LENS_CONFIG")
    if env:
        paths += [x.strip() for x in env.split(",") if x.strip()]
    paths.append(os.path.join(ROOT, "config.json"))
    # 多租户场景：每个用户有自己的 config.user.json（经 extra 传入），
    # 绝不能再叠加「根目录那份共享 config.user.json」，否则会把别人的 Key 串进来。
    if not skip_global_user:
        paths.append(os.path.join(ROOT, "config.user.json"))
    if extra:
        if isinstance(extra, str):
            extra = [x.strip() for x in extra.split(",") if x.strip()]
        paths += list(extra)
    return paths


def load_config(extra=None, require_provider=True, skip_global_user=False):
    """返回合并后的配置字典。缺失 provider 时给出可读报错。

    skip_global_user=True：不加载根目录共享的 config.user.json（多租户按用户隔离时用）。
    """
    cfg = dict(DEFAULTS)
    used = []
    for p in _candidate_paths(extra, skip_global_user=skip_global_user):
        if p and os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = _merge(cfg, json.load(f))
                used.append(p)
            except Exception as e:
                raise SystemExit(f"[config] 解析失败 {p}: {e}")
    cfg = _expand_env(cfg)
    cfg["_loaded_from"] = used
    if require_provider:
        if not cfg.get("providers"):
            raise SystemExit(
                "[config] 未配置任何 provider。\n"
                "请在 config.json（或 config.user.json）里填：\n"
                '  {"providers":[{"id":"any","base_url":"https://.../v1",'
                '"api_key":"sk-xxx","model":"..."}],"active_provider":"any"}\n'
                "可先运行：python scripts/llm_models.py --list  查看已配置项")
        cfg["active_provider"] = pick_active(cfg)
    return cfg


def pick_active(cfg):
    """确定生效的 provider id：显式指定且 enabled > 环境变量 > 第一个 enabled。"""
    provs = cfg.get("providers") or []
    want = cfg.get("active_provider") or os.environ.get("LITERATURE_LENS_PROVIDER") or ""
    by_id = {p.get("id"): p for p in provs}
    if want in by_id and by_id[want].get("enabled", True):
        return want
    enabled = [p for p in provs if p.get("enabled", True)]
    if enabled:
        # 权重高者优先，其次 roles 含 chat 的
        enabled.sort(key=lambda p: (-(p.get("weight") or 0),
                                    0 if "chat" in (p.get("roles") or []) else 1))
        return enabled[0].get("id")
    return provs[0].get("id") if provs else ""


def save_user_config(patch, path=None):
    """把补丁写进 config.user.json（不碰随包的 config.json）。"""
    path = path or os.path.join(ROOT, "config.user.json")
    cur = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                cur = json.load(f)
        except Exception:
            cur = {}
    cur = _merge(cur, patch)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    return path
