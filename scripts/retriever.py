# -*- coding: utf-8 -*-
"""
联网文献检索层 —— 让 Skill 不再只依赖本地上传的 PDF

实测结论（2026-09-21 本机实测，改代码前必读）
---------------------------------------------
| 源              | 状态 | 说明                                                        |
|-----------------|------|-------------------------------------------------------------|
| OpenAlex        | ✅   | 官方免费 API，无需 key。含摘要、被引数、**OA 全文 PDF 直链**。主力 |
| arXiv           | ✅   | 官方 API，可下载全文 PDF                                     |
| Crossref        | ✅   | 官方 API，元数据最全（DOI 权威）                             |
| Semantic Scholar| ⚠️   | 无 key 时频繁 429，需退避重试；可选                          |
| Europe PMC      | ⚠️   | 偶发超时；可选                                               |
| 知网 CNKI       | ❌   | 三个入口全部返回「安全验证」页（2154B），强制反爬，无公开 API   |
| 维普 CQVIP      | ❌   | HTTP 412 直接拦截                                            |

知网/维普的替代路径：**导出文件导入**。用户在知网/维普检索后导出
RefWorks / BibTeX / EndNote / NoteExpress 文件，用 `parse_cnki_refworks()` 等
解析器入库 —— 合规且可行（CNKI 本身支持批量导出）。

统一记录结构（与 pipeline.process_one 兼容）
--------------------------------------------
{
  "path":       本地 PDF 路径（无全文时为 ""）
  "filename":   文件名 / 无全文时用 <id>.meta
  "title", "authors", "year", "journal", "doi",
  "lang":       "英文" / "中文"
  "source":     "OpenAlex" / "arXiv" / "CNKI(导入)" ...
  "source_key": "web"
  "domain_dir": 检索式（作为分类目录）
  "abstract":   摘要（无全文时用它做摘要级分析）
  "web_url":    落地页
  "pdf_url":    OA 全文直链
  "cited_by":   被引数
  "external":   True
}

零依赖：仅标准库 urllib / xml.etree。
"""
import os
import re
import json
import time
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

# OpenAlex 礼貌政策：请求头带 mailto 会进「礼貌队列」，限速更宽松
# 可在 config.json 的 retrieval.mailto 里改成自己的邮箱
MAILTO = os.environ.get("OPENALEX_MAILTO", "literaturelens@example.com")
UA = f"LiteratureLens/1.0 (mailto:{MAILTO})"


class RetrievalError(Exception):
    pass


# ---------------------------------------------------------------- HTTP
def _get(url, timeout=30, headers=None, retries=3, sleep0=2.0):
    """带退避的 GET。429/5xx 重试，其他状态码直接抛。"""
    h = {"User-Agent": UA, "Accept": "application/json, text/*;q=0.9"}
    if headers:
        h.update(headers)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503, 504) and i < retries - 1:
                time.sleep(sleep0 * (i + 1))
                continue
            raise RetrievalError(f"{last}: {url[:120]}")
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
            if i < retries - 1:
                time.sleep(sleep0 * (i + 1))
                continue
            raise RetrievalError(f"{last}: {url[:120]}")
    raise RetrievalError(str(last))


def _get_json(url, **kw):
    return json.loads(_get(url, **kw).decode("utf-8"))


# ---------------------------------------------------------------- OpenAlex
OA_SELECT = ("id,doi,title,display_name,publication_year,publication_date,"
             "authorships,primary_location,best_oa_location,open_access,"
             "abstract_inverted_index,cited_by_count,type,language")


def _abstract_from_inverted(inv):
    """OpenAlex 的摘要是「倒排索引」：{词: [位置...]} → 还原成文本。"""
    if not inv or not isinstance(inv, dict):
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    if not pos:
        return ""
    return " ".join(pos[k] for k in sorted(pos))


def _norm_doi(doi):
    if not doi:
        return ""
    s = str(doi).strip()
    s = (s.replace("https://doi.org/", "")
          .replace("http://doi.org/", "")
          .replace("doi.org/", ""))
    # 题录导出里常见 "DOI: 10.xxxx"（CNKI / EndNote / RefWorks 都出现过）
    s = re.sub(r"^doi\s*[:：]\s*", "", s, flags=re.I)
    return s.strip()


def _work_to_rec(w, query=""):
    """OpenAlex work → 统一 rec。"""
    title = (w.get("title") or w.get("display_name") or "").strip()
    authors = "; ".join(
        (a.get("author") or {}).get("display_name", "")
        for a in (w.get("authorships") or [])
        if (a.get("author") or {}).get("display_name"))[:400]
    pl = w.get("primary_location") or {}
    src = pl.get("source") or {}
    bl = w.get("best_oa_location") or {}
    oa = w.get("open_access") or {}
    pdf_url = bl.get("pdf_url") or pl.get("pdf_url") or ""
    land = pl.get("landing_page_url") or oa.get("oa_url") or ""
    doi = _norm_doi(w.get("doi"))
    wid = (w.get("id") or "").rstrip("/").split("/")[-1] or (doi or "item")
    lang = "中文" if (w.get("language") or "").startswith("zh") else "英文"
    return {
        "path": "",
        "filename": f"{wid}.pdf",
        "title": title,
        "authors": authors,
        "year": str(w.get("publication_year") or ""),
        "journal": src.get("display_name") or "",
        "doi": doi,
        "lang": lang,
        "source": "OpenAlex",
        "source_key": "web",
        "domain_dir": query or "联网检索",
        "abstract": _abstract_from_inverted(w.get("abstract_inverted_index")),
        "web_url": land,
        "pdf_url": pdf_url,
        "cited_by": w.get("cited_by_count") or 0,
        "type": w.get("type") or "",
        "external": True,
    }


def search_openalex(query, per_page=10, year_from=None, year_to=None,
                    oa_only=False, lang=None, sort="relevance"):
    """OpenAlex 检索。sort: relevance | cited_by_count | publication_date"""
    if not query:
        return []
    params = {
        "search": query,
        "per-page": max(1, min(int(per_page), 200)),
        "select": OA_SELECT,
    }
    if sort == "cited":
        params["sort"] = "cited_by_count:desc"
    elif sort == "date":
        params["sort"] = "publication_date:desc"
    filters = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    if oa_only:
        filters.append("is_oa:true")
    if lang:
        filters.append(f"language:{lang}")
    if filters:
        params["filter"] = ",".join(filters)
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    data = _get_json(url, timeout=40)
    return [_work_to_rec(w, query) for w in data.get("results", [])]


# ---------------------------------------------------------------- arXiv
ARXIV_NS = {"a": "http://www.w3.org/2005/Atom"}


def search_arxiv(query, max_results=10, sort="relevance"):
    """arXiv 检索（Atom API），含 PDF 直链。"""
    params = {
        "search_query": f'all:{query}',
        "start": 0,
        "max_results": max(1, min(int(max_results), 100)),
        "sortBy": {"relevance": "relevance", "date": "submittedDate",
                   "cited": "relevance"}.get(sort, "relevance"),
        "sortOrder": "descending",
    }
    url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    raw = _get(url, timeout=40)
    root = ET.fromstring(raw)
    out = []
    for e in root.findall("a:entry", ARXIV_NS):
        def txt(tag):
            n = e.find(f"a:{tag}", ARXIV_NS)
            return (n.text or "").strip() if n is not None and n.text else ""
        aid = txt("id")
        arxid = aid.rstrip("/").split("/")[-1] if aid else ""
        pdf = ""
        for link in e.findall("a:link", ARXIV_NS):
            if link.get("title") == "pdf" or (link.get("type") == "application/pdf"):
                pdf = link.get("href") or ""
        doi = ""
        for link in e.findall("a:link", ARXIV_NS):
            if link.get("title") == "doi":
                doi = _norm_doi(link.get("href"))
        jref = ""
        jn = e.find("a:journal_ref", ARXIV_NS) if False else None
        # arXiv 的 journal_ref 在 arxiv 命名空间下
        for child in e:
            if child.tag.endswith("journal_ref") and child.text:
                jref = child.text.strip()
        pub = txt("published")[:4]
        out.append({
            "path": "", "filename": f"{arxid or 'arxiv'}.pdf",
            "title": re.sub(r"\s+", " ", txt("title")),
            "authors": "; ".join(a.find("a:name", ARXIV_NS).text.strip()
                                 for a in e.findall("a:author", ARXIV_NS)
                                 if a.find("a:name", ARXIV_NS) is not None),
            "year": pub, "journal": jref, "doi": doi,
            "lang": "英文", "source": "arXiv", "source_key": "web",
            "domain_dir": query or "联网检索",
            "abstract": re.sub(r"\s+", " ", txt("summary")),
            "web_url": aid, "pdf_url": pdf or (aid.replace("/abs/", "/pdf/") if aid else ""),
            "cited_by": 0, "type": "preprint", "external": True,
        })
    return out


# ---------------------------------------------------------------- Crossref
def search_crossref(query, rows=10, year_from=None, year_to=None):
    """Crossref 检索：DOI 与元数据最权威，但通常没有摘要与全文。"""
    params = {"query": query, "rows": max(1, min(int(rows), 100)),
              "select": "DOI,title,author,issued,container-title,abstract,URL,type"}
    f = []
    if year_from:
        f.append(f"from-pub-date:{year_from}-01-01")
    if year_to:
        f.append(f"until-pub-date:{year_to}-12-31")
    if f:
        params["filter"] = ",".join(f)
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    data = _get_json(url, timeout=40)
    out = []
    for it in (data.get("message", {}) or {}).get("items", []) or []:
        title = (it.get("title") or [""])[0] or ""
        au = "; ".join(
            " ".join(x for x in [a.get("given"), a.get("family")] if x)
            for a in (it.get("author") or []))
        issued = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
        year = str(issued[0]) if issued and issued[0] else ""
        # Crossref 摘要常带 JATS XML 标签
        abst = it.get("abstract") or ""
        abst = re.sub(r"<[^>]+>", " ", abst)
        abst = re.sub(r"\s+", " ", abst).strip()
        doi = it.get("DOI") or ""
        out.append({
            "path": "", "filename": f"{(doi or title)[:40].replace('/', '_')}.pdf",
            "title": title, "authors": au, "year": year,
            "journal": (it.get("container-title") or [""])[0] or "",
            "doi": doi, "lang": "英文", "source": "Crossref", "source_key": "web",
            "domain_dir": query or "联网检索", "abstract": abst,
            "web_url": it.get("URL") or "", "pdf_url": "", "cited_by": 0,
            "type": it.get("type") or "", "external": True,
        })
    return out


# ---------------------------------------------------------------- Semantic Scholar
def search_s2(query, limit=10, year_range=None):
    """Semantic Scholar。无 key 时易 429 → 退避重试；失败返回空列表而非抛错。"""
    if not query:
        return []
    params = {
        "query": query, "limit": max(1, min(int(limit), 100)),
        "fields": "title,abstract,year,authors,venue,externalIds,openAccessPdf,citationCount",
    }
    if year_range:
        params["year"] = year_range
    url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(params)
    try:
        data = _get_json(url, timeout=40, retries=4, sleep0=3.0)
    except Exception:
        return []
    out = []
    for p in (data.get("data") or []):
        ids = p.get("externalIds") or {}
        oa = p.get("openAccessPdf") or {}
        doi = ids.get("DOI") or ""
        out.append({
            "path": "", "filename": f"{(doi or p.get('title','s2'))[:40].replace('/', '_')}.pdf",
            "title": p.get("title") or "",
            "authors": "; ".join(a.get("name", "") for a in (p.get("authors") or [])),
            "year": str(p.get("year") or ""), "journal": p.get("venue") or "",
            "doi": doi, "lang": "英文", "source": "SemanticScholar", "source_key": "web",
            "domain_dir": query or "联网检索", "abstract": p.get("abstract") or "",
            "web_url": f"https://www.semanticscholar.org/paper/{p.get('paperId','')}",
            "pdf_url": oa.get("url") or "", "cited_by": p.get("citationCount") or 0,
            "type": "", "external": True,
        })
    return out


# ---------------------------------------------------------------- 聚合
SEARCHERS = {
    "openalex": lambda q, n, kw: search_openalex(q, per_page=n, **kw),
    "arxiv": lambda q, n, kw: search_arxiv(q, max_results=n, **kw),
    "crossref": lambda q, n, kw: search_crossref(q, rows=n, **kw),
    "s2": lambda q, n, kw: search_s2(q, limit=n, **kw),
}

SOURCE_LABELS = {
    "openalex": "OpenAlex", "arxiv": "arXiv",
    "crossref": "Crossref", "s2": "Semantic Scholar",
}

# 每个源支持的检索参数。search_all 会把公共 kw 按此表过滤后再下发，
# 否则 Crossref 收到 oa_only 之类的参数会直接 TypeError（真实 bug）。
SOURCE_KWARGS = {
    "openalex": {"year_from", "year_to", "oa_only", "lang", "sort"},
    "arxiv": {"sort"},
    "crossref": {"year_from", "year_to"},
    "s2": {"year_from", "year_to"},
}


def _filter_kwargs(source, kw):
    allowed = SOURCE_KWARGS.get(source, set())
    return {k: v for k, v in (kw or {}).items()
            if k in allowed and v is not None and v is not False}


def _dedup_key(r):
    """去重键：DOI 优先，其次归一化标题。"""
    doi = (r.get("doi") or "").lower().strip()
    if doi:
        return "doi:" + doi
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (r.get("title") or "").lower())
    return "t:" + t[:80]


# ---------------------------------------------------------------- 相关性闸门
# 真实踩过的坑：query="farmer irrigation water use behavior" 在 arXiv 上命中了
# 《Water Bridging Dynamics of Polymerase Chain Reaction》——只因含 "water"。
# 各源的 relevance 排序对英文长查询不可靠，必须自己按关键词重合度过滤，
# 否则会把无关文献喂给翻译/分析，白白烧掉 API 额度。
_STOP = {"the", "and", "for", "with", "from", "that", "this", "into",
         "onto", "upon", "over", "under", "about", "across", "among",
         "are", "was", "were", "has", "have", "had", "not", "but",
         "its", "their", "our", "can", "may", "via", "per"}


def query_tokens(query):
    """检索式 → 关键词集合（去停用词、去过短词）。

    中文词多为 2 字（农户/灌溉/用水），英文按 3 字母起算；
    两者混用同一个长度阈值会把中文检索式整个清空（真实踩过）。
    """
    toks = re.findall(r"[a-z0-9\u4e00-\u9fff]+", (query or "").lower())
    out = set()
    for t in toks:
        if t in _STOP:
            continue
        cjk = bool(re.search(r"[\u4e00-\u9fff]", t))
        if len(t) >= (2 if cjk else 3):
            out.add(t)
    return out


def relevance_score(rec, query):
    """命中几个检索关键词。返回 (命中数, 关键词总数)。"""
    toks = query_tokens(query)
    if not toks:
        return 0, 0
    hay = ((rec.get("title") or "") + " " + (rec.get("abstract") or "")).lower()
    hit = sum(1 for t in toks
              if t in hay or (len(t) > 5 and t.rstrip("e") in hay))
    return hit, len(toks)


def filter_relevant(recs, query, min_hits=2):
    """按关键词重合度过滤。min_hits=0 关闭。返回 (保留的, 被过滤的)。

    阈值会按检索式的词数收敛：need = min(min_hits, 关键词总数)，
    所以「农户 灌溉」这种两词检索式不会因为只命中 1 个就被误杀，
    而长检索式要求至少命中 2 个 —— 正是这一步挡掉了
    《Water Bridging Dynamics of Polymerase Chain Reaction》那种
    只靠一个泛词 water 混进来的噪声。
    """
    if not min_hits or not query:
        return list(recs), []
    keep, drop = [], []
    for r in recs:
        hit, total = relevance_score(r, query)
        r["relevance"] = f"{hit}/{total}"
        need = min(min_hits, total) if total else 0
        (keep if hit >= need else drop).append(r)
    return keep, drop


def search_all(query, sources=("openalex",), per_source=10, dedup=True,
               min_relevance=2, **kw):
    """跨源检索并去重。单源失败不影响其他源。

    min_relevance：标题+摘要至少命中几个检索关键词（0=不过滤）。
    默认 2（并按检索式词数收敛）——过滤「只因含一个泛词而命中」的无关文献。
    """
    out, seen, errors = [], set(), []
    for s in sources:
        fn = SEARCHERS.get(s)
        if not fn:
            errors.append(f"未知源 {s}")
            continue
        try:
            got = fn(query, per_source, _filter_kwargs(s, kw))
        except TypeError as e:
            errors.append(f"{s}: 参数不兼容 {e}")
            continue
        except Exception as e:
            errors.append(f"{s}: {type(e).__name__}: {str(e)[:110]}")
            continue
        for r in got:
            k = _dedup_key(r)
            if dedup and k in seen:
                continue
            seen.add(k)
            out.append(r)
    kept, dropped = filter_relevant(out, query, min_relevance)
    if dropped:
        errors.append(f"相关性过滤：剔除 {len(dropped)} 条"
                      f"（关键词 {query} 命中不足 {min_relevance}）")
    return kept, errors


# ---------------------------------------------------------------- PDF 下载
def download_pdf(url, dest_dir, filename=None, timeout=90, min_bytes=8000):
    """下载 OA 全文 PDF。返回本地路径；失败返回 None。

    注意：不少「PDF 直链」实际返回 HTML（出版商落地页/验证码页），
    必须校验文件头为 %PDF，避免把网页当成 PDF 喂给抽取器。
    """
    if not url:
        return None
    os.makedirs(dest_dir, exist_ok=True)
    if not filename:
        base = os.path.basename(urllib.parse.urlparse(url).path) or "paper.pdf"
        if not base.lower().endswith(".pdf"):
            base += ".pdf"
        filename = re.sub(r"[^\w\.\-]+", "_", base)[:120]
    dest = os.path.join(dest_dir, filename)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": "application/pdf,*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except Exception:
        return None
    if len(data) < min_bytes or not data[:5].startswith(b"%PDF"):
        return None
    with open(dest, "wb") as f:
        f.write(data)
    return dest


def resolve_pdf(rec, dest_dir, timeout=90, min_bytes=8000):
    """给一条联网记录补上本地 PDF；下载失败则保持 path 为空（走摘要级分析）。

    带复用缓存：`downloads/` 里已存在同名且文件头为 %PDF 的文件就直接用，
    重跑流水线时不必把同一篇文献再下一遍（每次几十 MB，很费时间）。
    """
    if rec.get("path") and os.path.isfile(rec["path"]):
        return rec["path"]
    fn = rec.get("filename") or ""
    if fn:
        cached = os.path.join(dest_dir, fn)
        if os.path.isfile(cached) and os.path.getsize(cached) >= min_bytes:
            with open(cached, "rb") as f:
                if f.read(5).startswith(b"%PDF"):
                    rec["path"] = cached
                    rec["fulltext"] = True
                    return cached
    url = rec.get("pdf_url") or ""
    if not url:
        return None
    p = download_pdf(url, dest_dir, filename=fn or None, timeout=timeout,
                     min_bytes=min_bytes)
    if p:
        rec["path"] = p
        rec["fulltext"] = True
    else:
        rec["fulltext"] = False
    return p


# ---------------------------------------------------------------- 导出文件导入（知网/维普）
#
# 知网与维普没有公开 API 且强制反爬（实测：CNKI 三个入口全部返回「安全验证」页，
# 维普返回 HTTP 412）。合规路径是：用户在站内检索后【批量导出】题录文件，
# 再用这里的解析器导入。CNKI 支持导出 RefWorks / EndNote / NoteExpress / BibTeX。

def parse_refworks(text):
    """解析 RefWorks 格式（CNKI / 维普 / Web of Science 通用导出）。

    形如：
        RT Journal Article
        A1 张三
        T1 论文标题
        JF 期刊名
        YR 2024
        DO 10.xxxx/yyy
        AB 摘要...
    """
    recs, cur = [], {}
    tag = None
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = re.match(r"^([A-Z0-9]{2})\s+(.*)$", line)
        if m:
            tag, val = m.group(1), m.group(2).strip()
        else:
            # 续行（摘要/关键词常换行）
            if tag and cur.get(tag):
                cur[tag] += " " + line.strip()
            continue
        if tag == "RT" and cur:
            recs.append(cur)
            cur = {}
        cur[tag] = (cur.get(tag, "") + " " + val).strip() if tag in cur else val
    if cur:
        recs.append(cur)

    out = []
    for r in recs:
        title = r.get("T1") or r.get("TI") or r.get("ST") or ""
        if not title:
            continue
        authors = r.get("A1") or r.get("AU") or r.get("A") or ""
        out.append({
            "path": "", "filename": re.sub(r"[^\w\.\-]+", "_", title)[:60] + ".pdf",
            "title": title,
            "authors": authors.replace(";", "; "),
            "year": (r.get("YR") or r.get("PY") or "")[:4],
            "journal": r.get("JF") or r.get("JO") or r.get("T2") or "",
            "doi": _norm_doi(r.get("DO") or r.get("DI") or ""),
            "lang": "中文" if re.search(r"[\u4e00-\u9fff]", title) else "英文",
            "source": r.get("DB") or "CNKI/维普 导入",
            "source_key": "import",
            "domain_dir": "导入题录",
            "abstract": r.get("AB") or r.get("N2") or "",
            "web_url": r.get("UR") or "", "pdf_url": "", "cited_by": 0,
            "type": r.get("RT") or "", "external": True,
        })
    return out


def parse_bibtex(text):
    """解析 BibTeX（CNKI / Google Scholar / arXiv 均可导出）。"""
    out = []
    for m in re.finditer(r"@(\w+)\s*\{([^,]+),\s*(.*?)\n\}", text or "", re.S):
        kind, key, body = m.group(1), m.group(2), m.group(3)

        def grab(field):
            mm = re.search(rf"\b{field}\s*=\s*[{{](.*?)[}}]", body, re.S)
            if mm:
                return re.sub(r"\s+", " ", mm.group(1)).strip()
            mm = re.search(rf'\b{field}\s*=\s*"(.*?)"', body, re.S)
            return re.sub(r"\s+", " ", mm.group(1)).strip() if mm else ""

        title = grab("title").strip("{}")
        if not title:
            continue
        authors = grab("author").replace(" and ", "; ")
        out.append({
            "path": "", "filename": re.sub(r"[^\w\.\-]+", "_", title)[:60] + ".pdf",
            "title": title, "authors": authors,
            "year": grab("year")[:4], "journal": grab("journal") or grab("booktitle"),
            "doi": _norm_doi(grab("doi")),
            "lang": "中文" if re.search(r"[\u4e00-\u9fff]", title) else "英文",
            "source": "BibTeX 导入", "source_key": "import",
            "domain_dir": "导入题录",
            "abstract": grab("abstract"), "web_url": grab("url"),
            "pdf_url": "", "cited_by": 0, "type": kind, "external": True,
        })
    return out


def parse_endnote(text):
    """解析 EndNote 导出（%0 开头、%X 摘要等）。

    EndNote 与 RefWorks 标签体系不同（%0/%A/%T/%J/%D/%R/%X），单独实现。
    """
    recs, cur, tag = [], {}, None
    for line in (text or "").splitlines():
        m = re.match(r"^%([A-Z0-9])\s?(.*)$", line)
        if m:
            t, val = m.group(1), m.group(2).strip()
            if t == "0" and cur:
                recs.append(cur)
                cur = {}
            tag = t
            cur[tag] = (cur.get(tag, "") + " " + val).strip() if tag in cur else val
        elif tag and cur.get(tag):
            cur[tag] += " " + line.strip()
    if cur:
        recs.append(cur)

    out = []
    for r in recs:
        title = r.get("T") or ""
        if not title:
            continue
        authors = "; ".join(r.get(k, "") for k in ("A",) if r.get(k))
        out.append({
            "path": "", "filename": re.sub(r"[^\w\.\-]+", "_", title)[:60] + ".pdf",
            "title": title, "authors": authors,
            "year": (r.get("D") or "")[:4], "journal": r.get("J") or r.get("B") or "",
            "doi": _norm_doi(r.get("R") or ""),
            "lang": "中文" if re.search(r"[\u4e00-\u9fff]", title) else "英文",
            "source": "EndNote 导入", "source_key": "import",
            "domain_dir": "导入题录", "abstract": r.get("X") or "",
            "web_url": r.get("U") or "", "pdf_url": "", "cited_by": 0,
            "type": r.get("0") or "", "external": True,
        })
    return out


def parse_export(text, fmt=None):
    """自动识别并解析导出文件。fmt: refworks / bibtex / endnote / auto"""
    t = text or ""
    if fmt == "refworks" or re.search(r"^\s*RT\s+\w", t, re.M):
        return parse_refworks(t)
    if fmt == "endnote" or re.search(r"^\s*%0\s", t, re.M):
        return parse_endnote(t)
    if fmt == "bibtex" or re.search(r"@\w+\s*\{", t):
        return parse_bibtex(t)
    # 兜底：都试一遍，取结果最多的
    best = []
    for fn in (parse_refworks, parse_endnote, parse_bibtex):
        got = fn(t)
        if len(got) > len(best):
            best = got
    return best


# ---------------------------------------------------------------- 便捷入口
class Retriever:
    """检索门面：聚合各源 + 下载全文 + 导入题录。"""

    def __init__(self, cache_dir=None):
        self.cache_dir = cache_dir

    def search(self, query, sources=("openalex",), per_source=10, **kw):
        recs, errors = search_all(query, sources=sources,
                                  per_source=per_source, **kw)
        return recs, errors

    def attach_pdfs(self, recs, dest_dir=None, only_with_pdf=False, log=None):
        """尝试给每条记录下载 OA 全文。返回 (有全文的, 仅摘要的)。"""
        dest_dir = dest_dir or self.cache_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "downloads")
        full, abst_only = [], []
        for r in recs:
            p = resolve_pdf(r, dest_dir)
            if p:
                full.append(r)
                if log:
                    log(f"    ↓ 全文 {os.path.basename(p)}")
            else:
                abst_only.append(r)
        if only_with_pdf:
            return full, abst_only
        return full, abst_only

    def from_export(self, path_or_text, fmt=None):
        """从知网/维普等导出的题录文件导入。"""
        if os.path.isfile(path_or_text):
            for enc in ("utf-8-sig", "gbk", "utf-8", "gb18030"):
                try:
                    text = open(path_or_text, encoding=enc, errors="ignore").read()
                    break
                except Exception:
                    text = ""
        else:
            text = path_or_text
        return parse_export(text, fmt)
