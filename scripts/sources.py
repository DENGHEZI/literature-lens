# -*- coding: utf-8 -*-
"""
文献源发现 + 元信息解析

两个数据源格式不同，分别适配：
  A) D:/LiyuAgent/data/raw/鲤鱼科研    —— 按子领域目录，PDF 文件名含标题，部分带元数据 json
  B) D:/农户用水行为_文献库            —— 中文OA文献/英文OA文献，文件名 = 序号_年份_来源_标题.pdf
     另配 文献总清单.csv（最权威）
"""
import os
import re
import csv
import json

# ---------------- 源 A：LiyuAgent raw ----------------

def discover_liyu_raw(root, limit_per_domain=None, domains=None):
    """扫描 LiyuAgent raw，返回文献列表。"""
    out = []
    if not os.path.isdir(root):
        return out
    for sub in sorted(os.listdir(root)):
        dpath = os.path.join(root, sub)
        if not os.path.isdir(dpath):
            continue
        if domains and sub not in domains:
            continue
        pdfdirs = []
        for dirpath, dirnames, filenames in os.walk(dpath):
            pdfs = [f for f in filenames if f.lower().endswith(".pdf")]
            if pdfs:
                pdfdirs.append((dirpath, pdfs))
        cnt = 0
        for dirpath, pdfs in pdfdirs:
            for f in sorted(pdfs):
                if limit_per_domain and cnt >= limit_per_domain:
                    break
                full = os.path.join(dirpath, f)
                out.append({
                    "path": full,
                    "filename": f,
                    "domain_dir": sub,
                    "source": "LiyuAgent raw",
                    "source_key": "liyu_raw",
                })
                cnt += 1
    return out


# ---------------- 源 B：农户用水行为_文献库 ----------------

CN_LIB_FILENAME_RX = re.compile(r"^(\d+)_(\d{4})_([^_]+)_(.+)\.pdf$", re.I)


def load_farmers_csv(lib_root):
    """读 文献总清单.csv，返回 {本地文件名: 元信息}。"""
    meta = {}
    p = os.path.join(lib_root, "文献总清单.csv")
    if not os.path.exists(p):
        return meta
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            fn = (row.get("本地文件名") or "").strip()
            if not fn:
                continue
            meta[fn] = {
                "title": (row.get("标题") or "").strip(),
                "authors": (row.get("作者") or "").strip(),
                "year": (row.get("年份") or "").strip(),
                "journal": (row.get("期刊/来源") or "").strip(),
                "doi": (row.get("DOI") or "").strip(),
                "lang": (row.get("语种") or "").strip(),
                "volume": (row.get("卷") or "").strip(),
                "issue": (row.get("期") or "").strip(),
                "pages": (row.get("页码") or "").strip(),
            }
    return meta


def discover_farmers(lib_root, limit=None, lang=None):
    """扫描农户用水行为文献库。lang: 中文 / 英文 / None=全部"""
    out = []
    csvmeta = load_farmers_csv(lib_root)
    for sub in ("中文OA文献", "英文OA文献", "logs\\_archive"):
        dpath = os.path.join(lib_root, sub)
        if not os.path.isdir(dpath):
            continue
        if lang == "中文" and "中文" not in sub:
            continue
        if lang == "英文" and "英文" not in sub:
            continue
        for f in sorted(os.listdir(dpath)):
            if not f.lower().endswith(".pdf"):
                continue
            if limit and len(out) >= limit:
                return out
            m = csvmeta.get(f) or {}
            if not m:
                # 从文件名兜底解析 序号_年份_来源_标题.pdf
                mm = CN_LIB_FILENAME_RX.match(f)
                if mm:
                    m = {"title": mm.group(4).replace("_", " "), "year": mm.group(2),
                         "journal": mm.group(3)}
                else:
                    m = {"title": os.path.splitext(f)[0]}
            out.append({
                "path": os.path.join(dpath, f),
                "filename": f,
                "domain_dir": sub,
                "source": "农户用水行为_文献库",
                "source_key": "farmers_water",
                **m,
            })
    return out


# ---------------- 通用元信息补全 ----------------

def guess_title_from_filename(fn):
    """从 PDF 文件名猜标题（去序号/年份/期刊前缀）。"""
    s = os.path.splitext(os.path.basename(fn))[0]
    s = re.sub(r"^\d+[_\-\.]\s*", "", s)
    s = re.sub(r"^\d{4}[_\-\.]\s*", "", s)
    s = re.sub(r"^(?:IF[\d\.]+_)?\d*[_\-\.]\s*", "", s)
    s = s.replace("_", " ").strip()
    return s


# 出版商/期刊版式中常见的“非期刊名”行（版权、许可证、投稿信息等噪音）
PUB_NOISE_RX = re.compile(
    r"(creative\s+commons|licen[cs]e|copyright|©|all\s+rights\s+reserved|"
    r"correspondence|open\s+access|public\s+domain|downloaded\s+from|"
    r"http[s]?://|doi\.org|received:|accepted:|published:|"
    r"this\s+article|the\s+author|permission|attribution|"
    r"^\s*research\s*$|^\s*article\s*$|^\s*review\s*$|^\s*original\s+article\s*$)",
    re.I)

# 已知出版社/期刊名（用于在首页定位期刊行）
#
# 注意：这里【绝不能】放 Water / Land / Agriculture / Plants / Science / Cell 之类
# 的单个泛化词——它们会匹配到论文标题本身（真实事故：标题
# "Water scarcity: A global hindrance to sustainable" 被 `Water` 分支命中，
# 期刊字段被填成了论文标题）。只保留「足够具体、不像普通英文句子」的期刊名模式。
KNOWN_VENUE_RX = re.compile(
    r"^(?:BMC\s+[\w\s&\-]{2,40}|"
    r"PLOS\s+(?:ONE|Computational\s+Biology|Genetics|Neglected\s+Tropical\s+Diseases)|"
    r"Frontiers\s+in\s+[\w\s&\-]{3,50}|"
    r"Journal\s+of\s+[\w\s&\-]{3,60}|"
    r"International\s+Journal\s+of\s+[\w\s&\-]{3,60}|"
    r"Nature\s+(?:[\w\s&\-]{2,30}(?:Letters|Reviews|Communications|Water))?|"
    r"Science\s+(?:Advances|of\s+the\s+Total\s+Environment|Direct)|"
    r"Cell\s+(?:Reports|Press)?|"
    r"Agricultural\s+Water\s+Management|"
    r"Water\s+Resources\s+(?:Research|Management)|"
    r"Cambridge\s+Prisms:\s*[\w\s&\-]{2,30}|"
    r"Environmental\s+(?:Science\s+&\s+Technology|Research\s+Letters|Chemistry\s+Letters)|"
    r"Sustainability|Horticulturae|Irrigation\s+Science|"
    r"Agronomy|Agricultural\s+Systems|Hydrology\s+and\s+Earth\s+System\s+Sciences)"
    r"[\w\s&\-:,\.()]{0,45}$",
    re.I)

def looks_like_title(s):
    """判断一行是不是论文标题（而非期刊名）。

    期刊名与标题都可能带冒号（"Cambridge Prisms: Water" vs
    "Water scarcity: A global hindrance to ..."），区别在于：
      期刊名冒号后通常只有 1-3 个词（子刊名）
      标题冒号后往往是一整句
    """
    s = (s or "").strip()
    if not s:
        return False
    # 冒号/破折号副标题：按冒号后的词数区分
    m = re.search(r"[:–—]\s*(.+)$", s)
    if m and len(m.group(1).split()) > 3:
        return True
    # "A/An/The ... of/for/to/in ..." 句式，典型标题
    if re.search(r"\b(?:A|An|The)\s+\w+\s+(?:of|for|to|in|among|between)\b", s, re.I):
        return True
    # 含研究动作词（analysis/review/dynamics...）且句子较长
    if len(s) > 45 and re.search(
            r"\b(?:dynamics|analysis|review|assessment|impact|effects?|"
            r"comparative|evaluation|case\s+study|strategies|hindrance)\b", s, re.I):
        return True
    return False

DOI_RX = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)")
EMAIL_RX = re.compile(r"[\w\.\-]+@[\w\.\-]+\.\w+")


def _clean_venue(v):
    """把 "刊名, 197 (2019) 109380. doi:10.xxxx" 这种串切成纯刊名。

    Elsevier / ScienceDirect 的 subject 字段常把「刊名 + 卷期页码 + DOI」写在一起，
    直接取用会让期刊字段变成一整句话（真实事故：期刊字段 =
    "Engineering Structures, 197 (2019) 109380. doi:10.1016/j.engstruct.2019.109380"）。
    """
    s = re.split(r"\bdoi\s*:?", str(v), flags=re.I)[0]
    # 去掉 ", 197 (2019) 109380" / ", Vol. 197 (2019)" 之类的卷期页码尾巴
    s = re.sub(r"[,;]?\s*(?:Vol\.?\s*)?\d+\s*[\(\[]\s*\d{4}\s*[\)\]].*$", "", s)
    s = re.sub(r"[,;]?\s*\d{4}\s*[;,].*$", "", s)
    s = s.strip(" .,;:")
    return s if 3 < len(s) < 90 else ""


def _looks_like_filename_title(title, stem):
    """判断 title 是不是「从文件名硬猜」的产物（哈希串 / 与文件名主干一致）。

    微信/网盘导出的文件名常带一长串 md5 前缀（如
    "371ba335..._8fafa547..._8钢-UHPC(1).pdf"），guess_title_from_filename()
    会把这串哈希当成标题写进 doc，而 enrich 只在 title 为空时才补 ——
    结果标题永远是哈希串。这里反向识别这种垃圾标题。
    """
    s = (title or "").strip()
    if not s:
        return True
    if stem and s == stem:
        return True
    if re.search(r"\b[0-9a-f]{16,}\b", s, re.I):
        return True
    for part in re.split(r"[_\-\s(]+", stem or ""):
        if len(part) >= 20 and part in s:
            return True
    return False


def _journal_from_pdf_meta(pdfmeta):
    """优先从 PDF 内嵌元数据取期刊/标题线索。"""
    if not isinstance(pdfmeta, dict):
        return ""
    keys = ("subject", "journal", "publication", "title", "creator", "producer")
    for k in keys:
        v = pdfmeta.get(k)
        if not v:
            continue
        v = str(v).strip()
        # PDF 的 subject 经常直接写期刊名（可能带卷期/DOI，先清洗）
        if k in ("subject", "journal", "publication") and 3 < len(v) < 200:
            v2 = _clean_venue(v)
            if v2:
                return v2
    return ""


def enrich_from_pdf_firstpage(pages, doc, pdfmeta=None):
    """从首页文本（及 PDF 内嵌元数据）补全标题/作者/年份/期刊/DOI。"""
    if not pages:
        return doc
    t = pages[0].get("text") or ""
    lines = [l.strip() for l in t.split("\n") if l.strip()]

    # --- 标题 ---
    # 注意：title 可能已被 guess_title_from_filename() 填成文件名（含哈希串），
    # 不能只看「是否为空」——垃圾标题也要用 PDF 内嵌元数据 / 首页标题行替换。
    fn_stem = os.path.splitext(os.path.basename(doc.get("filename") or ""))[0]
    if _looks_like_filename_title(doc.get("title"), fn_stem):
        cand = ""
        if isinstance(pdfmeta, dict) and pdfmeta.get("title"):
            cand = str(pdfmeta["title"]).strip()
        if len(cand) < 15 and lines:
            c = max(lines[:8], key=len)
            if len(c) > 15:
                cand = c
        if cand and not _looks_like_filename_title(cand, fn_stem):
            doc["title"] = cand

    # --- 年份 ---
    if not doc.get("year"):
        m = re.search(r"\b(19|20)\d{2}\b", t)
        if m:
            doc["year"] = m.group(0)

    # --- DOI ---
    if not doc.get("doi"):
        m = DOI_RX.search(t)
        if not m and isinstance(pdfmeta, dict):
            # ScienceDirect 正文页常不印 DOI，只在元数据 subject 里带
            blob = " ".join(str(pdfmeta.get(k) or "") for k in
                            ("subject", "title", "keywords", "identifier"))
            m = DOI_RX.search(blob)
        if m:
            doc["doi"] = m.group(1).rstrip(".,;)")

    # --- 期刊 ---
    if not doc.get("journal"):
        j = ""
        # 1) PDF 内嵌元数据
        j = _journal_from_pdf_meta(pdfmeta)
        # 2) 首页里匹配已知期刊名，跳过版权/许可证噪音行
        #    并排除「长得像论文标题」的行（带冒号副标题、A ... of ... 句式等）
        if not j:
            for l in lines:
                s = l.strip()
                if len(s) < 4 or len(s) > 90:
                    continue
                if PUB_NOISE_RX.search(s):
                    continue
                if KNOWN_VENUE_RX.match(s) and not looks_like_title(s):
                    j = s
                    break
        # 3) 兜底：单独成行、含 “Journal / 期刊语汇” 且不像正文的短行
        if not j:
            for l in lines[:60]:
                s = l.strip()
                if not (4 < len(s) < 80):
                    continue
                if PUB_NOISE_RX.search(s) or s.endswith((".", "!", "?")):
                    continue
                if re.search(r"\b(Journal|Review|Bulletin|Annals|Proceedings|"
                             r"Letters|Transactions|Advances|Archives)\b", s, re.I):
                    j = s
                    break
        if j:
            doc["journal"] = j

    # --- 作者（无 CSV 元数据时，取通讯作者邮箱前的姓名行） ---
    if not doc.get("authors"):
        m = EMAIL_RX.search(t)
        if m:
            # 邮箱上方 2 行内找姓名
            for i, l in enumerate(lines):
                if m.group(0) in l:
                    for j in range(max(0, i - 2), i + 1):
                        cand = lines[j].strip()
                        if re.fullmatch(r"[A-Z][A-Za-z\.\-']+(?:\s+[A-Z][A-Za-z\.\-']+){1,3}", cand):
                            doc["authors"] = cand
                            break
                    break
    # 兜底：PDF 内嵌 author 字段（Elsevier 首页不印邮箱时唯一来源）
    if not doc.get("authors") and isinstance(pdfmeta, dict) and pdfmeta.get("author"):
        a = str(pdfmeta["author"]).strip()
        if 2 < len(a) < 300:
            doc["authors"] = a
    return doc
