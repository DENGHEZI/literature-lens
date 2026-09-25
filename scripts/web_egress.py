# -*- coding: utf-8 -*-
"""
统一出网（代理接管）+ 通用学术网页检索 + 翻译置信度

设计要点
--------
1. 接管用户访问互联网的地址：所有外网请求（翻译 API、学术检索）都由本服务统一出口。
   通过把代理写进进程级环境变量 HTTP_PROXY/HTTPS_PROXY/NO_PROXY，
   让标准库 urllib（CloudLLM 与下面的 web_search 都走 urllib）统一走代理。
2. 通用学术网页检索：对译文短语做网页检索，按命中率 + 学术来源占比，
   给出 0-100 的翻译置信度，并附「学术检索依据」链接供人工核对。
3. 全程 best-effort：检索失败/超时不影响翻译本身，置信度返回 None 或降级提示。

零依赖：仅标准库 urllib。
"""
import os
import re
import json
import time
import urllib.request
import urllib.error
import urllib.parse

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 学术域名信号：结果 URL 命中其一即视为学术来源（提升置信度权重）
ACADEMIC_DOMAINS = (
    "arxiv.org", "scholar.google", "researchgate.net", "semanticscholar.org",
    "doi.org", "pubmed.ncbi", "ncbi.nlm.nih", "ieee.org", "springer.com",
    "sciencedirect.com", "wiley.com", "tandfonline.com", "acm.org",
    "nature.com", "ssrn.com", ".edu", ".ac.", ".gov", "researchsquare.com",
    "frontiersin.org", "mdpi.com", "biomedcentral.com", "jstor.org",
    "sciencedirect", "core.ac.uk", "researchgate",
)

# 检索提供方默认值：bing 在国内免 Key、免代理可直接访问，作为默认与兜底
DEFAULT_PROVIDER = "bing"


# ---------------------------------------------------------------- 代理接管
def apply_proxy(egress):
    """接管出网地址：把代理写进进程级环境变量，urllib 全服务统一走代理。

    返回实际生效的环境变量字典（便于调试 / 热更新）。
    """
    env = {}
    if not isinstance(egress, dict):
        return env
    prox = egress.get("proxy") or {}
    http_p = (prox.get("http") or egress.get("http_proxy") or "").strip()
    https_p = (prox.get("https") or egress.get("https_proxy") or "").strip()
    no_p = (prox.get("no_proxy") or egress.get("no_proxy") or "").strip()

    if http_p:
        os.environ["HTTP_PROXY"] = http_p
        os.environ["http_proxy"] = http_p
        env["HTTP_PROXY"] = http_p
        env["http_proxy"] = http_p
    if https_p:
        os.environ["HTTPS_PROXY"] = https_p
        os.environ["https_proxy"] = https_p
        env["HTTPS_PROXY"] = https_p
        env["https_proxy"] = https_p
    if no_p:
        os.environ["NO_PROXY"] = no_p
        os.environ["no_proxy"] = no_p
        env["NO_PROXY"] = no_p
        env["no_proxy"] = no_p
    return env


def egress_cfg(egress, key, default=None):
    if not isinstance(egress, dict):
        return default
    return egress.get(key, default)


# ---------------------------------------------------------------- 底层 GET
def _get(url, timeout=12, headers=None, data=None, method="GET"):
    hdrs = {"User-Agent": DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if isinstance(headers, dict):
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
        return body, r.geturl(), r.headers.get("Content-Type", "")


# ---------------------------------------------------------------- 检索解析
def _strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&[a-z]+;", " ", s)
    s = re.sub(r"\s+", " ", s)
    return urllib.parse.unquote(s).strip()


def _ddg_real_url(href):
    """DuckDuckGo 把外链包成 //duckduckgo.com/l/?uddg=ENCODED，解出真实地址。"""
    if not href:
        return ""
    m = re.search(r"uddg=([^&]+)", href)
    if m:
        return urllib.parse.unquote(m.group(1))
    if href.startswith("//"):
        return "https:" + href
    return href


def _is_academic(url):
    u = (url or "").lower()
    return any(d in u for d in ACADEMIC_DOMAINS)


def _parse_results(html, provider, n, base_url=""):
    items = []
    if provider == "duckduckgo":
        links = re.findall(r'class="result__a"[^>]*href="([^"]+)"', html)
        titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.S)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
        for i, href in enumerate(links[:n]):
            title = _strip_tags(titles[i]) if i < len(titles) else ""
            snip = _strip_tags(snippets[i]) if i < len(snippets) else ""
            items.append({"url": _ddg_real_url(href), "title": title, "snippet": snip})
    elif provider in ("bing", "google"):
        blocks = re.findall(r'<li class="b_algo".*?</li>', html, re.S)
        if not blocks:  # google 用不同结构兜底
            blocks = re.findall(r'<div class="g".*?</div>\s*</div>', html, re.S)
        for b in blocks[:n]:
            href = re.search(r'<a[^>]+href="(https?://[^"]+)"', b)
            title = re.search(r'<h3[^>]*>(.*?)</h3>', b, re.S)
            snip = re.search(r'<(?:p|span)[^>]*>(.*?)</(?:p|span)>', b, re.S)
            if href:
                items.append({
                    "url": href.group(1),
                    "title": _strip_tags(title.group(1)) if title else "",
                    "snippet": _strip_tags(snip.group(1)) if snip else "",
                })
    else:  # 通用兜底：抓所有外链 + 周围文本
        for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.S):
            items.append({"url": m.group(1), "title": _strip_tags(m.group(2)), "snippet": ""})

    seen, out = set(), []
    for it in items:
        url = it.get("url") or ""
        if not url or url in seen:
            continue
        if url.startswith("https://duckduckgo.com") or url.startswith("https://www.bing.com") \
           or url.startswith("https://www.google.com"):
            continue  # 过滤搜索引擎自身链接
        seen.add(url)
        it["academic"] = _is_academic(url)
        out.append(it)
    return out[:n]


# ---------------------------------------------------------------- 报错人话化
def friendly_err(e):
    """把 urllib 的底层报错翻译成用户能直接行动的提示。"""
    s = f"{type(e).__name__}: {e}"
    low = s.lower()
    if "10061" in s or "connection refused" in low or " actively refused" in low:
        return "代理端口连不上（代理软件没开？）——可清空代理直连，或先开启代理"
    if "timed out" in low or "10060" in s or "timeout" in low:
        return "连接超时（该网站可能被墙/拦截，或网络不通；建议换 Bing 或填可用代理）"
    if "getaddrinfo failed" in low or "11001" in s or "name or service not known" in low:
        return "域名解析失败（DNS/网络不通）"
    if "403" in s:
        return "被检索源拒绝(403)，建议换提供方（Bing）或配置代理"
    if "429" in s:
        return "检索源限流(429)，稍后再试或换提供方"
    return s[:180]


# ---------------------------------------------------------------- 对外检索
def web_search(query, n=8, egress=None, timeout=None, allow_fallback=True):
    """通用学术网页检索。返回 [{url,title,snippet,academic}]，失败抛异常。

    国内友好：默认 Bing；首选源失败/无结果时自动回退 Bing 一次，
    回退情况记录在 web_search.last_note（供测试端点展示）。
    """
    web_search.last_note = ""
    prov = egress_cfg(egress, "search_provider", DEFAULT_PROVIDER) or DEFAULT_PROVIDER
    if timeout is None:
        timeout = egress_cfg(egress, "search_timeout", 10) or 10

    def _one(p):
        q = urllib.parse.quote(query)
        if p == "duckduckgo":
            url = "https://html.duckduckgo.com/html/?q=" + q
        elif p == "bing":
            url = "https://www.bing.com/search?q=" + q + "&count=" + str(n)
        elif p == "google":
            url = "https://www.google.com/search?q=" + q + "&num=" + str(n)
        elif p == "custom":
            tpl = egress_cfg(egress, "search_url", "") or "https://html.duckduckgo.com/html/?q={q}"
            url = tpl.replace("{n}", str(n)).replace("{q}", q)
        else:
            url = "https://www.bing.com/search?q=" + q + "&count=" + str(n)
        headers = {"User-Agent": DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        html, final, _ = _get(url, timeout=timeout, headers=headers)
        return _parse_results(html, p, n, final)

    try:
        res = _one(prov)
    except Exception as e:
        if allow_fallback and prov != "bing":
            try:
                res = _one("bing")
                web_search.last_note = f"首选 {prov} 失败（{friendly_err(e)}），已自动回退 Bing"
                prov = "bing"
            except Exception as e2:
                raise RuntimeError(friendly_err(e2)) from e2
        else:
            raise RuntimeError(friendly_err(e)) from e
    if not res and allow_fallback and prov not in ("bing", "custom"):
        try:
            res = _one("bing")
            web_search.last_note = f"{prov} 无结果，已自动回退 Bing"
            prov = "bing"
        except Exception:
            pass
    return res


# ---------------------------------------------------------------- 置信度
def translation_confidence(source, target, egress=None, timeout=None):
    """用学术网页检索为译文提供置信度。

    返回 {"score": int|None, "level": "...", "evidence": [...], "note": "..."}
    全程 best-effort：任何异常都返回降级结果，绝不抛到翻译主流程。
    """
    try:
        src = (source or "").strip()
        tgt = (target or "").strip()
        if not tgt:
            return {"score": None, "level": "unknown", "evidence": [],
                    "note": "译文为空，无法评估"}
        # 取译文第一段 / 前 80 字作为检索短语（避免整段噪声）
        phrase = tgt.split("\n")[0].strip()
        if len(phrase) > 80:
            phrase = phrase[:80]
        # 去掉首尾标点，取核心 4~40 字用于命中判定
        core = phrase.strip("，。；：、,.!?;:()（）\"'")
        if len(core) < 4:
            return {"score": None, "level": "unknown", "evidence": [],
                    "note": "译文过短，无法检索印证"}
        q = '"' + core + '"'
        res = web_search(q, n=8, egress=egress, timeout=timeout)
        if not res:
            return {"score": 35, "level": "low", "evidence": [],
                    "note": "未检索到交叉印证，置信度偏低（建议人工核对术语）"}
        total = len(res)
        hits = [r for r in res
                if core[:max(4, len(core) // 2)] in (r.get("title", "") + r.get("snippet", ""))]
        academic_hits = [r for r in hits if r.get("academic")]
        hit_ratio = min(1.0, len(hits) / max(1, total))
        acad_ratio = (len(academic_hits) / max(1, len(hits))) if hits else 0.0
        # 35 基础分 + 50×命中率 + 15×学术占比
        score = int(round(35 + 50 * hit_ratio + 15 * acad_ratio))
        score = max(5, min(99, score))
        level = "high" if score >= 70 else ("medium" if score >= 50 else "low")
        evidence = [{
            "url": r["url"], "title": (r.get("title") or r.get("url"))[:120],
            "snippet": (r.get("snippet") or "")[:160], "academic": bool(r.get("academic")),
        } for r in (hits or res)[:5]]
        note = ("检索到 %d 条结果，其中 %d 条含该译文；%d 条来自学术来源。"
                % (total, len(hits), len(academic_hits)))
        return {"score": score, "level": level, "evidence": evidence, "note": note}
    except Exception as e:
        return {"score": None, "level": "unknown", "evidence": [],
                "note": "置信度检索失败：" + str(e)[:120]}


# 便于调试：直接运行 `python web_egress.py "query"` 看检索结果
if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "concrete compressive strength"
    cfg = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    try:
        r = web_search(q, n=5, egress=cfg)
        print("provider:", cfg.get("search_provider", DEFAULT_PROVIDER))
        print("results:", len(r))
        for x in r:
            print(" -", ("[学] " if x["academic"] else "    "), x["title"][:60], "->", x["url"][:70])
    except Exception as e:
        print("ERR:", type(e).__name__, e)
