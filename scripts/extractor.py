# -*- coding: utf-8 -*-
"""
PDF / 文档抽取层
- 优先 PyMuPDF(fitz)，缺失时回退 pypdf，再回退纯 Python 文本兜底
- 按「段落」切块，识别章节标题，保留页码用于溯源
- 针对学术 PDF：合并断词连字符、过滤页眉页脚、保留公式行原样
"""
import os
import re

# 学术论文里常见的章节标题
SECTION_RX = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\s*[\.、]?\s*)?"
    r"(abstract|introduction|related work|background|method(?:s|ology)?|"
    r"experiment(?:s|al)?|result(?:s)?|discussion|conclusion(?:s)?|"
    r"摘要|引言|绪论|研究背景|文献综述|相关工作|研究方法|方法|材料与方法|"
    r"试验|实验|结果与分析|结果|讨论|结论|参考文献|致谢)"
    r"\s*(?:\n|$)",
    re.I)

# 编号 + 标题 且标题后紧跟正文（同一块内）→ 拆成 heading + para
HEADING_SPLIT_RX = re.compile(
    r"^(?P<num>\d+(?:\.\d+)*)\s*\n?"
    r"(?P<title>Abstract|Introduction|Related work|Background|Methods?|Methodology|"
    r"Experiments?|Experimental|Results?|Discussion|Conclusions?|References)\b"
    r"\s*\n(?P<rest>\S.+)$",
    re.I | re.S)

# 标题行：全大写短行 / 论文标题特征
TITLE_LIKE_RX = re.compile(r"^[A-Z][^\n]{10,160}$")


def _split_heading_inline(text):
    """把标题与正文粘连的块拆开，支持多级级联标题。

    "3\\nMaterials and methods\\n3.1\\nDatasets and models\\n正文..."
       -> (["3 Materials and methods", "3.1 Datasets and models"], "正文...")
    "1\\nIntroduction\\n正文..."                      -> (["1 Introduction"], "正文...")
    纯标题块 "3 Materials and methods"                -> (["3 Materials and methods"], "")
    """
    s = (text or "").strip()
    if not s:
        return None

    lines = [l.strip() for l in s.split("\n")]
    lines = [l for l in lines if l]

    heads, i = [], 0
    # 逐行嗅探标题：编号行 / 编号+标题行 / 纯标题行
    while i < len(lines):
        ln = lines[i]
        # 纯编号行，如 "3" / "3.1"
        if re.fullmatch(r"\d+(?:\.\d+)*", ln) and i + 1 < len(lines):
            nxt = lines[i + 1]
            if _is_heading_word(nxt) or (len(nxt) < 60 and nxt[:1].isupper()):
                heads.append(f"{ln} {nxt}".strip())
                i += 2
                continue
        # 编号+标题同一行，如 "3 Materials and methods" / "1 模型构建与研究假说"
        m = re.match(r"^(\d+(?:\.\d+){0,3})\s+(\S.{0,70})$", ln)
        if m:
            title = m.group(2)
            # 标题特征：短、不含句末标点、不以连词/标点开头
            if (len(title) <= 45 and not re.search(r"[。；，,\.;：:]$", title)
                    and not re.match(r"^[的地得和与及或是在为对于基于]", title)
                    and (_is_heading_word(title)
                         or (len(title) <= 22 and not re.search(r"[。！？；]", title)
                             and re.search(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}", title)))):
                heads.append(f"{m.group(1)} {title}".strip())
                i += 1
                continue
        # 无编号的经典章节名
        if _is_heading_word(ln) and len(ln) < 45:
            heads.append(ln)
            i += 1
            continue
        break

    rest = "\n".join(lines[i:]).strip()
    if not heads:
        return None
    if not rest:
        return (heads, "")
    return (heads, rest)


HEADWORD = re.compile(
    r"^(?:abstract|introduction|related work|background|method(?:s|ology)?|"
    r"materials? and methods?|experiments?(?: and materials)?|experimental(?: design| setup)?|"
    r"results?(?: and discussion)?|discussion|conclusions?|references|"
    r"datasets?(?: and models?)?|data(?: and| collection)?|models?|"
    r"evaluation|analysis|limitations?|acknowledg(?:e)?ments?|appendix|"
    r"摘要|引言|绪论|研究背景|文献综述|相关工作|研究方法|研究方法与数据|方法|材料与方法|"
    r"数据与方法|数据来源|试验设计|实验设计|试验|实验|结果与分析|结果|讨论|结论|参考文献|致谢|"
    r"[A-Z][A-Za-z \-]{2,45})$", re.I)


def _is_heading_word(s):
    s = (s or "").strip()
    if not s or len(s) > 60:
        return False
    # 公式/变量碎片：短、含下标符号、无实义词
    if len(s) <= 12:
        toks = re.findall(r"[A-Za-z\u4e00-\u9fff]+", s)
        if not toks:
            return False
        # 形如 "i t" / "ln GS" / "GP" 的碎片一律不是标题
        if len(toks) <= 2 and sum(len(t) for t in toks) <= 6:
            return False
    # 含大量单字母 token 的也不是标题
    toks = re.findall(r"[A-Za-z]+", s)
    if toks and sum(1 for t in toks if len(t) == 1) / len(toks) > 0.5:
        return False
    if len(s) <= 10 and not re.search(r"[\u4e00-\u9fff]", s):
        return False
    return bool(HEADWORD.match(s))

HEADER_FOOTER_RX = re.compile(
    r"^\s*(?:page\s*\d+|\d+\s*/\s*\d+|第\s*\d+\s*页|"
    r"vol\.?\s*\d+.*?\d{4}|doi:\s*10\.\S+)\s*$", re.I)

# 期刊页眉/页脚整行：页码 + 刊名 + 卷期年，或纯期刊页码区间
JOURNAL_RUNNING_RX = re.compile(
    r"^\s*(?:\d{1,5}\s*$|"                                   # 纯页码 1628
    r"\d{1,5}\s+\d{1,3}\s*$|"                                # 1 3 / 1 2（Springer 页脚标记）
    r"Vol\.?:?\(?\d*\)?\s*$|"                                # Vol.:(0123456789)
    r"[A-Z][A-Za-z&\.\s]{3,60}\(\d{4}\)\s*\d{1,4}\s*[:：]?\s*\d{1,5}\s*[-–]\s*\d{1,5}\s*|"  # 刊名(年) 卷:起-止
    r"[A-Z][A-Za-z&\.\s]{3,60}\s+\d{1,3}\s*[:：]\s*\d{1,5}\s*[-–]\s*\d{1,5}|"              # 刊名 卷:起-止
    r"doi:?\s*10\.\d{4,9}/\S+|"
    r"https?://doi\.org/\S+)\s*$", re.I)

# 连续这些行凑成的块，整体视为噪音
JOURNAL_META_HINT = re.compile(
    r"(Received|Accepted|Published|Corresponding author|"
    r"REVIEW\s*PAPER|ORIGINAL\s*PAPER|RESEARCH\s*ARTICLE|REVIEW\s*ARTICLE)",
    re.I)


def _strip_running_heads(text):
    """逐行剔除页眉页脚（纯页码、刊名卷期、Springer 页脚标记）。"""
    keep = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            keep.append(ln)
            continue
        if HEADER_FOOTER_RX.match(s) or JOURNAL_RUNNING_RX.match(s):
            continue
        # "Environmental Chemistry Letters (2023) 21:1627–1657 1 3" 这类合并行
        if re.match(r"^[A-Z][A-Za-z&\.\s]{3,60}\(\d{4}\)", s) and re.search(r"\d{1,4}\s*[:：]\s*\d{1,5}", s):
            continue
        keep.append(ln)
    return "\n".join(keep)

# 中文期刊首页/页眉的元数据噪音行（不做翻译与分析）
META_NOISE_RX = re.compile(
    r"(基金项目|Foundation item|作者简介|Corresponding author|通讯作者|"
    r"收稿日期|Received\s+\d|Accepted\s+\d|中图分类号|文献标识码|文章编号|"
    r"第\s*\d+\s*卷|Vol\.?\s*\d+|No\.?\s*\d+|第\s*\d+\s*期|"
    r"ISSN|CN\s*\d|E-?mail[:：]|DOI[:：]|网络首发|引用格式|"
    r"RESEARCH OF|JOURNAL OF|^\s*[A-Z][A-Za-z ]{2,40}\s*$)",
    re.I)


def _is_meta_noise(s):
    """判断是否是首页/页眉元数据噪音块。"""
    if not s:
        return True
    if META_NOISE_RX.search(s[:220]):
        return True
    # 纯数字/页码碎片
    if re.fullmatch(r"[\d\s\-—/·.、]+", s):
        return True
    return False


def _clean_text(raw):
    """学术 PDF 文本清洗：断词、多余空白、页眉页脚、软换行。"""
    t = raw or ""
    # 连字符断行：hyphen-\nword -> hyphenword
    t = re.sub(r"-\n(?=[a-z])", "", t)
    # 单换行（同段落内）-> 空格；双换行保留为段落
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    lines = t.split("\n")
    out = []
    for ln in lines:
        s = ln.strip()
        if s and HEADER_FOOTER_RX.match(s):
            continue
        if s and JOURNAL_RUNNING_RX.match(s):
            continue
        out.append(ln)
    t = "\n".join(out)
    # 再清理合并在一行的刊名卷期页眉
    t = "\n".join(
        ln for ln in t.split("\n")
        if not (re.match(r"^[A-Z][A-Za-z&\.\s]{3,60}\(\d{4}\)", ln.strip())
                and re.search(r"\d{1,4}\s*[:：]\s*\d{1,5}", ln))
    )
    return t.strip()


def extract_pdf(path, max_pages=None):
    """返回 {"pages":[{"page":n,"text":...}], "engine":str, "n_pages":int}"""
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    # --- 1) PyMuPDF ---
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(path)
        n = doc.page_count if not max_pages else min(doc.page_count, max_pages)
        pages = []
        for i in range(n):
            pages.append({"page": i + 1, "text": _clean_text(doc.load_page(i).get_text("text"))})
        total = doc.page_count
        try:
            pdfmeta = dict(doc.metadata or {})
        except Exception:
            pdfmeta = {}
        doc.close()
        return {"pages": pages, "engine": "pymupdf", "n_pages": total, "pdfmeta": pdfmeta}
    except ImportError:
        pass
    except Exception:
        pass

    # --- 2) pypdf ---
    try:
        from pypdf import PdfReader
        r = PdfReader(path)
        total = len(r.pages)
        n = total if not max_pages else min(total, max_pages)
        pages = []
        for i in range(n):
            try:
                txt = r.pages[i].extract_text() or ""
            except Exception:
                txt = ""
            pages.append({"page": i + 1, "text": _clean_text(txt)})
        try:
            pdfmeta = dict(r.metadata or {})
        except Exception:
            pdfmeta = {}
        return {"pages": pages, "engine": "pypdf", "n_pages": total, "pdfmeta": pdfmeta}
    except ImportError:
        pass
    except Exception:
        pass

    raise RuntimeError(
        "没有可用的 PDF 解析库。请安装其中一个（任选）：\n"
        "  pip install pymupdf\n"
        "  pip install pypdf")


def extract_text(path):
    """txt / md 直接读，返回单页结构。"""
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return {"pages": [{"page": 1, "text": _clean_text(f.read())}],
                        "engine": "text", "n_pages": 1}
        except UnicodeDecodeError:
            continue
        except Exception:
            break
    raise RuntimeError(f"无法读取文本文件：{path}")


def extract(path, max_pages=None):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return extract_pdf(path, max_pages=max_pages)
    if ext in (".txt", ".md", ".markdown", ".csv"):
        return extract_text(path)
    # 未知后缀，先按 PDF 试，再按文本试
    try:
        return extract_pdf(path, max_pages=max_pages)
    except Exception:
        return extract_text(path)


# ---------------- 原页渲染（保留图片/公式排版） ----------------

def render_page_images(path, out_dir, zoom=1.25, quality=58, max_pages=None):
    """把 PDF 逐页渲染成 JPG，供网页「原文分屏」展示原版式与图表。

    纯文本抽取会丢掉论文里最重要的图（.water footprint 地图、系统框图、
    回归表）——用户明确要求保留。这里直接渲染整页位图，图、公式、
    双栏排版全部原样保留。

    体积实测（zoom 1.25 / q58）：约 110-130KB/页。base64 内联后再涨 33%，
    一篇 8 页文献约 1MB —— 这是单文件网页可接受的上限，别再调高参数。
    返回 [{"page":1,"file":路径,"w":宽,"h":高}]；非 PDF 或渲染失败返回 []。
    """
    try:
        import fitz
    except ImportError:
        return []
    os.makedirs(out_dir, exist_ok=True)
    out = []
    try:
        doc = fitz.open(path)
    except Exception:
        return []
    n = doc.page_count if max_pages is None else min(doc.page_count, max_pages)
    for i in range(n):
        try:
            pm = doc.load_page(i).get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                                             alpha=False)
            fp = os.path.join(out_dir, f"p{i + 1}.jpg")
            with open(fp, "wb") as f:
                f.write(pm.tobytes("jpeg", jpg_quality=quality))
            out.append({"page": i + 1, "file": fp,
                        "w": pm.width, "h": pm.height})
        except Exception:
            continue
    doc.close()
    return out


# ---- 词级文本层（划词翻译）----
WORD_MAX_PER_PAGE = 2500      # 单页词数上限：防超密页把网页体积撑爆
WORD_MIN_BOX = 0.3            # 归一化后千分比最小尺寸，滤掉噪点框
WORD_MIN_FOR_SCAN = 25        # 少于这些词 + 页内有图 → 判为扫描页


def _column_gutters(words):
    """找分栏中缝：统计每个 x（0-1000 刻度）被多少词覆盖，
    覆盖数显著低于两侧的连续竖条就是栏间空白。

    不用「相邻 x0 的最大间隙」判——中文期刊的中缝只有 2% 宽，
    而左栏内部因为每行首词缩进不同，x0 间隙反而更大，会误判。
    这里看的是「有哪些 x 位置几乎没有字站着」，中英文都稳。
    """
    if len(words) < 60:
        return []
    grid = [0] * 1000
    for w in words:
        a = max(0, min(999, int(w[0])))
        b = max(0, min(999, int(w[2])))
        for x in range(a, b + 1):
            grid[x] += 1
    busy = max(2, int(len(words) * 0.15))     # 低于这个覆盖数算「空白」
    # 通栏标题会横跨整页，但只有少数几行，用比例阈值就能忽略它
    spans, run = [], None
    for x in range(1000):
        if grid[x] < busy:
            run = [x, x] if run is None else [run[0], x]
        else:
            if run and run[1] - run[0] >= 15:
                spans.append(tuple(run))
            run = None
    if run and run[1] - run[0] >= 15:
        spans.append(tuple(run))
    # 只保留页面中部（10%-90%）的中缝，页边空白不算
    gut = [s for s in spans if s[0] > 100 and s[1] < 900]
    gut.sort(key=lambda s: s[1] - s[0], reverse=True)
    return gut[:2]                             # 最多支持 3 栏


def _split_columns(words, depth=0):
    """按中缝切分栏（最多 3 栏）。双栏期刊按流顺序输出会让划选出的文字
    左右栏交叉，必须先分栏：整体顺序 = 左栏 → 右栏。"""
    if depth >= 2 or len(words) < 60:
        return [words]
    gut = _column_gutters(words)
    if not gut:
        return [words]
    a, b = gut[0]
    mid = (a + b) / 2.0
    left = [w for w in words if w[0] < mid]
    right = [w for w in words if w[0] >= mid]
    if len(left) < len(words) * 0.12 or len(right) < len(words) * 0.12:
        return [words]
    return _split_columns(left, depth + 1) + _split_columns(right, depth + 1)


def _sort_reading(words):
    """阅读顺序：先分栏（左→右），栏内按行（上→下），行内按 x（左→右）。"""
    if len(words) < 3:
        return words
    hs = sorted(w[3] - w[1] for w in words)
    tol = max((hs[len(hs) // 2] or 6.0) * 0.6, 2.0)
    out = []
    for col in _split_columns(words):
        rows = []
        for w in sorted(col, key=lambda z: (z[1], z[0])):
            if rows and (w[1] - rows[-1][0][1]) <= tol:
                rows[-1].append(w)
            else:
                rows.append([w])
        for r in rows:
            out.extend(sorted(r, key=lambda z: z[0]))
    return out


def _page_words(page):
    """抽一页的词框（已归一化 + 按阅读顺序）。返回 (词列表, 备注 dict)。

    适配点（都是真机上踩出来的）：
      - 旋转页：get_text 给的是未旋转坐标，而渲染是旋转后的，
        不换算会整页错位。实测 page.rotation_matrix 对齐后 0 越界
        （raw 同页越界 35-208 个词）。变换后若仍大量越界则回退 raw。
      - 坐标越界/NaN/零尺寸：裁剪到 0-1000 并丢弃噪点框。
      - 扫描页：词极少且页内有图 → 标记 scanned，前端提示无法划词。
      - 超密页：超过 WORD_MAX_PER_PAGE 截断并标记 truncated。
    """
    import fitz  # 调用方保证可用
    rect = page.rect
    w = float(rect.width) or 1.0
    h = float(rect.height) or 1.0
    note = {}
    raw = []
    try:
        raw = page.get_text("words") or []
    except Exception as e:
        note["err"] = f"{type(e).__name__}: {e}"[:60]
        return [], note

    pts = []
    for x0, y0, x1, y1, word, *_ in raw:
        if not word or not word.strip():
            continue
        pts.append((float(x0), float(y0), float(x1), float(y1), word))
    if not pts:
        return [], note

    if page.rotation:
        note["rotated"] = True
        m = page.rotation_matrix
        try:
            cand = []
            for x0, y0, x1, y1, word in pts:
                p0 = fitz.Point(x0, y0) * m
                p1 = fitz.Point(x1, y1) * m
                # rotation 后 x0/x1 可能互换、y0/y1 同理
                cand.append((min(p0.x, p1.x), min(p0.y, p1.y),
                             max(p0.x, p1.x), max(p0.y, p1.y), word))
            oob = sum(1 for c in cand
                      if c[0] < -2 or c[1] < -2 or c[2] > w + 2 or c[3] > h + 2)
            if oob <= len(cand) * 0.02:      # 对齐成功才用，否则不如不用
                pts = cand
            else:
                note["rot_unaligned"] = True
        except Exception:
            note["rot_unaligned"] = True

    ws = []
    for x0, y0, x1, y1, word in pts:
        try:
            a = _clamp(x0 * 1000 / w)
            b = _clamp(y0 * 1000 / h)
            c = _clamp(x1 * 1000 / w)
            d = _clamp(y1 * 1000 / h)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if c - a < WORD_MIN_BOX or d - b < WORD_MIN_BOX:
            continue
        ws.append([round(a), round(b), round(c), round(d), word])

    if len(ws) > WORD_MAX_PER_PAGE:
        note["truncated"] = len(ws) - WORD_MAX_PER_PAGE
        ws = ws[:WORD_MAX_PER_PAGE]
    if len(ws) < WORD_MIN_FOR_SCAN:
        try:
            if page.get_images(full=True):
                note["scanned"] = True
        except Exception:
            pass
    return _sort_reading(ws), note


def _clamp(v, lo=0.0, hi=1000.0):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return lo
    if v != v:            # NaN
        return lo
    return lo if v < lo else (hi if v > hi else v)


def extract_word_boxes(path, page_numbers, with_meta=False):
    """提取指定页的词级文本框，供原文分屏「划词翻译」用。

    图片本身选不了文字，这里把 PDF 真实单词连同坐标一起交给前端，
    模板在页图上盖一层透明文字（跟 PDF.js 的 text layer 同理），
    用户就能像小绿鲸那样划选 → 弹工具条 → 翻译。

    坐标存「千分比」（相对页宽/高，0-1000 整数），与渲染 zoom 解耦，
    以后改 zoom 也不会错位。词序按阅读顺序（分栏 → 分行 → 行内左右），
    保证划出来的文字是通顺的一句话而不是左右栏乱穿。

    返回 {页码(str): [[x0,y0,x1,y1,"词"], ...]}；
    with_meta=True 时返回 (词表, meta)，meta 记每页的适配情况
    （rotated / scanned / truncated / no_text / err），供前端提示。
    """
    try:
        import fitz
    except ImportError:
        return ({}, {}) if with_meta else {}
    out, meta = {}, {
        "engine": "pymupdf",
        "scanned_pages": [], "rotated_pages": [],
        "truncated_pages": [], "no_text_pages": [], "err": "",
    }
    try:
        doc = fitz.open(path)
    except Exception as e:
        meta["err"] = f"打不开：{type(e).__name__}: {e}"[:80]
        return (out, meta) if with_meta else out
    # 加密 PDF：先试空密码（多数"加密"其实是可打印的空口令）
    if getattr(doc, "needs_pass", False):
        if not doc.authenticate(""):
            meta["err"] = "PDF 已加密且空密码解不开，需要口令"
            doc.close()
            return (out, meta) if with_meta else out
        meta["encrypted"] = True
    for pno in page_numbers or []:
        i = int(pno) - 1
        if i < 0 or i >= doc.page_count:
            continue
        try:
            page = doc.load_page(i)
            ws, nt = _page_words(page)
        except Exception as e:
            meta.setdefault("err_pages", []).append(str(int(pno)))
            meta["err"] = meta["err"] or f"{type(e).__name__}: {e}"[:80]
            continue
        if ws:
            out[str(int(pno))] = ws
        key = str(int(pno))
        if nt.get("rotated"):
            meta["rotated_pages"].append(key)
        if nt.get("scanned"):
            meta["scanned_pages"].append(key)
        if nt.get("truncated"):
            meta["truncated_pages"].append(key)
        if not ws:
            meta["no_text_pages"].append(key)
    doc.close()
    return (out, meta) if with_meta else out


# ---------------- 分块 ----------------

def split_blocks(pages, min_len=60, scope="body"):
    """把逐页文本切成「块」：{idx, page, kind, text}

    kind: title | heading | formula | para
    scope: body=只正文 / all=含参考文献
    """
    blocks = []
    idx = 0
    in_refs = False

    def push(page, kind, text):
        nonlocal idx
        blocks.append({"idx": idx, "page": page, "kind": kind, "text": text})
        idx += 1

    for pg in pages:
        pno = pg["page"]
        text = pg.get("text") or ""
        if not text:
            continue
        if re.search(r"^\s*(references|bibliography|参考文献)\s*$", text, re.I | re.M):
            in_refs = True
        paras = re.split(r"\n\s*\n", text)
        if len(paras) == 1:
            paras = re.split(r"(?<=[。！？\.])\s*\n", text)

        for p in paras:
            s = p.strip()
            if not s:
                continue
            if in_refs and scope == "body":
                continue

            # ---- 噪音过滤（页眉元数据/基金/作者简介/纯页码） ----
            if _is_meta_noise(s):
                continue

            # ---- 标题 / 章节标题与正文粘连 → 先拆开 ----
            sp = _split_heading_inline(s)
            if sp:
                heads, rest = sp
                for h in heads:
                    push(pno, "heading", h)
                if not rest:
                    continue
                s = rest
                if _is_meta_noise(s):
                    continue

            if _looks_like_formula(s):
                push(pno, "formula", s)
                continue

            # 首页大标题（很可能是论文标题）
            if not blocks and pno == 1 and len(s) < 300:
                lines = [l.strip() for l in s.split("\n") if l.strip()]
                if lines and TITLE_LIKE_RX.match(lines[0]) and len(lines[0]) > 15:
                    push(pno, "title", lines[0])
                    rest = " ".join(lines[1:]).strip()
                    if len(rest) > min_len:
                        push(pno, "para", rest)
                    continue

            if len(s) < min_len:
                if blocks and blocks[-1]["page"] == pno and blocks[-1]["kind"] == "para":
                    blocks[-1]["text"] += " " + s
                    continue
                if len(s) < 25:
                    continue
            push(pno, "para", s)

    return blocks


FORMULA_HINT = re.compile(
    r"(?:^|\s)(?:[A-Za-z]\s*=\s*|[∑∫∂√≈≤≥∞][^\n]{0,80}$|[A-Za-z]_\{[a-z0-9]+\}|"
    r"\\frac|\\sum|\\int|\^\{|\$[^$]{4,}\$)")


def _looks_like_formula(s):
    """粗判公式行：短、含大量数学符号、或含 LaTeX 片段、或纯变量行。"""
    if len(s) > 400:
        return False
    signs = sum(s.count(c) for c in "=∑∫∂√≈≤≥±×÷∈∀∃∇−⌀")
    if signs >= 2:
        return True
    if FORMULA_HINT.search(s):
        return True
    latex_ops = sum(s.count(k) for k in ("\\frac", "\\sum", "\\int", "^{", "_{"))
    if latex_ops >= 2:
        return True
    # 中文期刊公式特征：（数字）编号行
    if re.search(r"（\d+[a-z]?）\s*$", s) and len(s) < 300:
        # 但含完整中文句子的（如假说 "H2：…。"）不算公式
        if not re.search(r"[\u4e00-\u9fff]{6,}[。！？]", s):
            return True
    # 纯变量/符号行：极短且无可读词
    t = s.strip()
    if len(t) <= 40:
        # 含成句中文 → 是正文
        if re.search(r"[\u4e00-\u9fff]", t):
            return False
        words = re.findall(r"[A-Za-z\u4e00-\u9fff]{3,}", t)
        if not words:
            return True
        # 单字母占比极高
        allt = re.findall(r"[A-Za-z]+", t)
        if allt and sum(1 for x in allt if len(x) == 1) / len(allt) > 0.6:
            return True
    return False


def chunk_for_translate(blocks, max_chars=2600):
    """把块合并成翻译批次，尽量按页聚合，控制单次 prompt 长度。"""
    batches, cur, cur_len, cur_page = [], [], 0, None
    for b in blocks:
        if b["kind"] == "formula":
            # 公式单独成批，避免被模型改写
            continue
        blen = len(b["text"])
        if cur and (cur_len + blen > max_chars or b["page"] != cur_page):
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(b)
        cur_len += blen
        cur_page = b["page"]
    if cur:
        batches.append(cur)
    return batches
