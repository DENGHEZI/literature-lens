# -*- coding: utf-8 -*-
"""
文献透镜主流水线 —— 模型无关版

  发现文献 -> 抽取正文 -> 结构化翻译 -> 创新点分析 -> 知识点生成 -> 汇总 JSON

与旧版的区别：
  * 配置走 config_loader（支持 config.user.json 覆盖 + 环境变量引用 key）
  * provider 按【角色】路由：translate / analyze 可分别用不同模型
  * 限定参数来自 config.limits，不再散落硬编码
  * 单个 provider 连续失败时可自动切换备用 provider（fallback）

用法：
  python pipeline.py --source all --limit 3
  python pipeline.py --source all --limit 3 --provider kimi
  python pipeline.py --source farmers --limit 2 --lang 中文
  python pipeline.py --source all --limit 3 --tr-provider glm --az-provider deepseek
"""
import os
import sys
import json
import time
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from extractor import extract, split_blocks, render_page_images, extract_word_boxes
from llm_client import CloudLLM, LLMError
from config_loader import load_config
from translator import Translator, build_bilingual
from analyzer import Analyzer, build_meta, pick_body_blocks
from sources import (discover_liyu_raw, discover_farmers,
                     guess_title_from_filename, enrich_from_pdf_firstpage)

CONFIG = load_config()
ROOT = os.path.dirname(HERE)
OUT_DIR = CONFIG["output"]["dir"]
DATA_DIR = os.path.join(ROOT, "data")
LIM = CONFIG.get("limits") or {}

# 原页图片（保留图表/公式排版，供网页「原文分屏」用）
PI_CFG = CONFIG.get("pages_images") or {}
PI_ON = bool(PI_CFG.get("enabled", True))
PI_ZOOM = float(PI_CFG.get("zoom", 1.25))
PI_QUALITY = int(PI_CFG.get("quality", 58))
# 词级文本层（划词翻译用）；坐标是千分比，与 zoom 无关
PI_WORDS = bool(PI_CFG.get("words", True))


def log(*a):
    print("  ", *a, flush=True)


# ---------------- 单篇处理 ----------------

def process_one(rec, llm, tr, az, max_pages=None):
    t0 = time.time()
    path = rec["path"]
    log(f"抽取：{rec['filename'][:60]}")
    d = extract(path, max_pages=max_pages)
    pages = d["pages"]
    blocks = split_blocks(pages, scope="body")

    did = _make_id({"filename": rec["filename"], "path": path})

    # ---- 原页渲染：纯文本抽取会丢掉图/公式/双栏排版，这里整页存 JPG ----
    pages_img = {}
    pages_txt = {}
    pages_txt_meta = {}
    if PI_ON:
        try:
            imgs = render_page_images(
                path, os.path.join(DATA_DIR, "pages", did),
                zoom=PI_ZOOM, quality=PI_QUALITY,
                max_pages=max_pages if max_pages is not None
                else int(LIM.get("max_pages", 8)))
            pages_img = {str(p["page"]): os.path.relpath(p["file"], ROOT)
                         .replace("\\", "/") for p in imgs}
            if imgs:
                log(f"  原页图片 {len(imgs)} 张")
            # ---- 词级文本层：原文分屏「划选 → 翻译」的数据来源 ----
            if PI_WORDS and imgs:
                try:
                    pages_txt, pages_txt_meta = extract_word_boxes(
                        path, [p["page"] for p in imgs], with_meta=True)
                    if pages_txt:
                        n_w = sum(len(v) for v in pages_txt.values())
                        log(f"  词级文本层 {n_w} 词（划词翻译用）")
                    for k in ("scanned_pages", "rotated_pages",
                              "truncated_pages", "no_text_pages"):
                        if (pages_txt_meta or {}).get(k):
                            log(f"  页适配 · {k}: {pages_txt_meta[k]}")
                    if (pages_txt_meta or {}).get("err"):
                        log(f"  页适配 · 警告: {pages_txt_meta['err']}")
                except Exception as e:
                    log(f"  文本层提取失败（不影响其他流程）：{type(e).__name__}: {e}")
        except Exception as e:
            log(f"  原页渲染失败（不影响文本流程）：{type(e).__name__}: {e}")

    doc = {
        "title": rec.get("title") or guess_title_from_filename(rec["filename"]),
        "authors": rec.get("authors", ""),
        "year": rec.get("year", ""),
        "journal": rec.get("journal", ""),
        "doi": rec.get("doi", ""),
        "lang_src": "zh" if (rec.get("lang") == "中文" or _is_chinese(rec.get("title", ""))) else "en",
        "source": rec.get("source", ""),
        "source_key": rec.get("source_key", ""),
        "domain_dir": rec.get("domain_dir", ""),
        "filename": rec["filename"],
        "path": path,
    }
    doc = enrich_from_pdf_firstpage(pages, doc, pdfmeta=d.get("pdfmeta"))

    # ---- 中文文献：原生就是中文，翻译层改为单栏，避免无意义中译中 ----
    is_cn = doc["lang_src"] == "zh"
    body_blocks = [b for b in blocks if b["kind"] != "formula"]
    log(f"  块数 {len(blocks)}，可译 {len(body_blocks)}，语言 {doc['lang_src']}")

    bilingual, terms = [], []
    if not is_cn:
        log("  抽术语 ...")
        sample = _term_sample(body_blocks,
                              max_chars=int(LIM.get("max_chars_terms", 6000)))
        try:
            terms = tr.extract_terms(sample, limit=30)
        except LLMError as e:
            log("  术语抽取失败，跳过：", str(e)[:80])
        log(f"  翻译 {len(body_blocks)} 段 ...")

        def prog(i, n, err=None):
            log(f"    批次 {i}/{n}" + (f"  [警告] {err}" if err else ""))
        got = tr.translate_blocks(body_blocks, terms=terms, progress=prog)
        bilingual = build_bilingual(blocks, got)
    else:
        bilingual = [{"idx": b["idx"], "page": b["page"], "kind": b["kind"],
                      "source": b["text"], "target": "",
                      "keep_original": b["kind"] == "formula"} for b in blocks]

    log("  创新点分析 ...")
    body_text = pick_body_blocks(blocks,
                                 max_chars=int(LIM.get("max_chars_body", 14000)),
                                 exclude_abstract=True)
    analysis = None
    try:
        analysis = az.analyze(build_meta(doc), body_text)
    except LLMError as e:
        log("  分析失败：", str(e)[:100])

    if not analysis:
        analysis = {"profile": {}, "insights": [], "concepts": []}

    return {
        "id": _make_id(doc),
        "pages_img": pages_img,
        "pages_txt": pages_txt,
        "pages_txt_meta": pages_txt_meta,
        "meta": doc,
        "stats": {
            "n_pages": d["n_pages"],
            "n_blocks": len(blocks),
            "n_terms": len(terms),
            "engine": d["engine"],
            "elapsed_s": round(time.time() - t0, 1),
        },
        "terms": terms,
        "bilingual": bilingual,
        "profile": analysis["profile"],
        "insights": analysis["insights"],
        "concepts": analysis["concepts"],
    }


def process_web_one(rec, llm, tr, az, max_pages=None, translate_abstract=True):
    """处理一条联网检索来的记录。

    两种深度：
      fulltext —— 拿到了 OA 全文 PDF，走完整流水线（翻译+分析）
      abstract —— 只有摘要，做「摘要级分析」：中英对照展示摘要 + 基于摘要提炼创新点

    摘要级条目在 meta 里带 `abstract_only=True`，prompt 会明确要求模型
    「只依据摘要、不得编造」，避免把摘要级结果吹成全文级结论。
    """
    if rec.get("path") and os.path.isfile(rec["path"]):
        d = process_one(rec, llm, tr, az, max_pages=max_pages)
        d["depth"] = "fulltext"
        d["meta"]["web_url"] = rec.get("web_url", "")
        d["meta"]["cited_by"] = rec.get("cited_by", 0)
        d["meta"]["source"] = rec.get("source", "")
        return d

    abstract = (rec.get("abstract") or "").strip()
    doc = dict(rec)
    doc.setdefault("meta", {})
    bilingual = []
    if abstract and translate_abstract and doc.get("lang") != "中文":
        zh = tr.translate_one({"idx": 0, "text": abstract, "kind": "para"})
        bilingual = [{"idx": 0, "page": 0, "kind": "para",
                      "source": abstract, "target": zh or "", "keep_original": False}]
    elif abstract:
        bilingual = [{"idx": 0, "page": 0, "kind": "para", "source": abstract,
                      "target": "", "keep_original": False}]

    analysis = None
    if abstract:
        mdoc = dict(rec)
        mdoc["abstract_only"] = True
        try:
            analysis = az.analyze(build_meta(mdoc), abstract[:6000])
        except LLMError as e:
            log("  摘要级分析失败：", str(e)[:100])
    if not analysis:
        analysis = {"profile": {}, "insights": [], "concepts": []}
    # 摘要级没有页码。分析器可能顺着提示词填成 "1"，UI 上会显示成 p1，
    # 让人误以为引自全文第 1 页 —— 统一改成「摘要」。
    for it in analysis.get("insights", []):
        if it.get("page") in (None, "", 0, 1, "1"):
            it["page"] = "摘要"

    return {
        "id": _make_id(rec),
        "depth": "abstract",
        "pages_img": {},
        "meta": {
            "title": rec.get("title", ""),
            "authors": rec.get("authors", ""),
            "year": rec.get("year", ""),
            "journal": rec.get("journal", ""),
            "doi": rec.get("doi", ""),
            "lang_src": "zh" if rec.get("lang") == "中文" else "en",
            "source": rec.get("source", ""),
            "source_key": "web",
            "domain_dir": rec.get("domain_dir", ""),
            "filename": rec.get("filename", ""),
            "path": rec.get("path", ""),
            "abstract": abstract,
            "web_url": rec.get("web_url", ""),
            "cited_by": rec.get("cited_by", 0),
            "abstract_only": True,
        },
        "stats": {"n_pages": 0, "n_blocks": len(bilingual),
                  "n_terms": 0, "engine": "abstract-only",
                  "elapsed_s": 0},
        "terms": [],
        "bilingual": bilingual,
        "profile": analysis["profile"],
        "insights": analysis["insights"],
        "concepts": analysis["concepts"],
    }


def _term_sample(blocks, max_chars=6000):
    """术语抽样：头 + 中 + 尾，而不是只取开头。

    只取前 10 段时，术语几乎全来自引言/摘要，方法段与结果段的核心术语
    （如 deficit irrigation、water productivity）会被漏掉 —— 实测同一篇
    论文只取开头得 9 个术语，头中尾取样得 30 个。
    """
    if not blocks:
        return ""
    n = len(blocks)
    if n <= 12:
        pick = list(range(n))
    else:
        h = max(3, n // 4)
        pick = list(range(0, h)) + list(range(n // 2 - 3, n // 2 + 3)) \
            + list(range(n - h, n))
    out, size = [], 0
    for i in pick:
        if 0 <= i < n:
            t = blocks[i]["text"]
            if size + len(t) > max_chars and out:
                break
            out.append(t)
            size += len(t)
    return "\n".join(out)


def _is_chinese(s):
    if not s:
        return False
    cn = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return cn / max(1, len(s)) > 0.25


def _make_id(doc):
    """按【文件名】生成稳定 id。

    早期版本用 md5(文件名+标题)，但标题会被 CSV 元数据补全/修正
    → id 随之改变 → 同一篇文献重跑后变成两条重复数据。
    改用文件名做键：文件名是唯一且不变的，元数据怎么补全都不影响去重。
    文件名为空（上传未带原名）时改用文件内容哈希：
    否则全部撞成 md5('')=d41d8cd9，多篇互 相覆盖。
    """
    import hashlib
    fn = (doc.get("filename") or "").strip()
    if fn:
        return hashlib.md5(fn.encode("utf-8")).hexdigest()[:8]
    fp = doc.get("path")
    if fp and os.path.isfile(fp):
        h = hashlib.md5()
        with open(fp, "rb") as f:
            h.update(f.read(262144))   # 前 256KB 足够区分
        return h.hexdigest()[:8]
    import uuid
    return uuid.uuid4().hex[:8]


# ---------------- 主流程 ----------------

def build_engine(tr_pid=None, az_pid=None):
    """按角色装配「翻译引擎」与「分析引擎」，可各自指向不同模型。"""
    llm = CloudLLM(CONFIG.get("providers"), active=CONFIG.get("active_provider"))
    if not tr_pid:
        tr_pid = (llm.pick_for("translate") or {}).get("id")
    if not az_pid:
        az_pid = (llm.pick_for("analyze") or {}).get("id")
    tr = Translator(llm, max_chars=CONFIG["translate"]["max_chars_per_block"], pid=tr_pid)
    az = Analyzer(llm, pid=az_pid)
    return llm, tr, az, tr_pid, az_pid


def discover_web(query, sources=("openalex",), per_source=10, oa_only=False,
                 year_from=None, year_to=None, sort="relevance",
                 min_relevance=1):
    """联网检索并尽量抓取 OA 全文。返回 (recs, 报告)"""
    from retriever import search_all, resolve_pdf
    recs, errors = search_all(query, sources=sources, per_source=per_source,
                              oa_only=oa_only, year_from=year_from,
                              year_to=year_to, sort=sort,
                              min_relevance=min_relevance)
    dest = os.path.join(ROOT, "downloads")
    n_full = 0
    for r in recs:
        if resolve_pdf(r, dest):
            n_full += 1
    return recs, {"errors": errors, "n_total": len(recs), "n_fulltext": n_full}


def discover_import(path, fmt=None):
    """从知网/维普等导出的题录文件导入（它们没有公开 API，只能走导出导入）。"""
    from retriever import Retriever
    recs = Retriever().from_export(path, fmt)
    return recs, {"errors": [], "n_total": len(recs), "n_fulltext": 0}


def run(source="all", limit=3, lang=None, tag="", max_pages=None, out=None,
        tr_pid=None, az_pid=None, query=None, web_sources=("openalex",),
        import_file=None, oa_only=False, year_from=None, year_to=None,
        web_sort="relevance", abstract_only_ok=True, min_relevance=2):
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    if max_pages is None:
        max_pages = int(LIM.get("max_pages", 8))

    recs, report = [], {"errors": []}
    if source in ("liyu_raw", "all"):
        recs += discover_liyu_raw(CONFIG["sources"]["liyu_raw"], limit_per_domain=limit)
    if source in ("farmers", "all"):
        recs += discover_farmers(CONFIG["sources"]["farmers_water"], limit=limit, lang=lang)
    if source == "web" or query:
        if not query:
            log("联网检索需要 --query")
        else:
            got, rep = discover_web(query, sources=web_sources, per_source=limit,
                                    oa_only=oa_only, year_from=year_from,
                                    year_to=year_to, sort=web_sort,
                                    min_relevance=min_relevance)
            recs += got
            report = rep
            print(f"[net] 检索「{query}」→ {rep['n_total']} 条，"
                  f"其中 {rep['n_fulltext']} 条拿到 OA 全文")
            for e in rep.get("errors", []):
                print(f"      [源失败] {e}")
    if source == "import" or import_file:
        got, rep = discover_import(import_file or "", None)
        recs += got
        report = rep
        print(f"[import] 从题录文件导入 {len(got)} 条")

    if not recs:
        log("没有发现文献，请检查路径 / 检索式")
        return None

    # 联网/导入的记录：若没有全文且不允许摘要级，则剔除
    if not abstract_only_ok:
        before = len(recs)
        recs = [r for r in recs if r.get("path") and os.path.isfile(r["path"])]
        print(f"[net] 仅保留有全文的：{before} → {len(recs)}")

    print(f"[+] 共 {len(recs)} 篇待处理")
    llm, tr, az, tr_pid, az_pid = build_engine(tr_pid, az_pid)
    tname = (llm.provider(tr_pid) or {}).get("name")
    aname = (llm.provider(az_pid) or {}).get("name")
    print(f"[+] 翻译模型 = {tname}   分析模型 = {aname}   （云端 API，未使用本地模型）")

    docs, failed = [], []
    for i, r in enumerate(recs, 1):
        print(f"\n[{i}/{len(recs)}] {r['filename'][:70]}")
        try:
            if r.get("external"):
                docs.append(process_web_one(r, llm, tr, az, max_pages=max_pages))
            else:
                docs.append(process_one(r, llm, tr, az, max_pages=max_pages))
        except Exception as e:
            log("  !! 失败：", f"{type(e).__name__}: {e}"[:160])
            failed.append({"filename": r["filename"],
                           "error": f"{type(e).__name__}: {e}"[:200]})

    n_full = sum(1 for d in docs if d.get("depth") != "abstract")
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": aname,
        "models": {"translate": tname, "analyze": aname},
        "provider": {"translate": tr_pid, "analyze": az_pid},
        "model_note": "全部由云端 API 模型完成，未调用本地 Ollama / llama-server",
        "query": query or "",
        "retrieval": report,
        "stats": {
            "n_docs": len(docs),
            "n_fulltext": n_full,
            "n_abstract": len(docs) - n_full,
            "n_failed": len(failed),
            "llm_calls": llm.stats["calls"],
            "by_provider": llm.stats.get("by_provider", {}),
            "insights": sum(len(d["insights"]) for d in docs),
            "concepts": sum(len(d["concepts"]) for d in docs),
        },
        "failed": failed,
        "docs": docs,
    }
    name = f"lens_data{('_' + tag) if tag else ''}.json"
    out_path = out or os.path.join(DATA_DIR, name)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n[+] 数据已写入 {out_path}")
    print(f"[+] 统计 {payload['stats']}")
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="all",
                    choices=["all", "liyu_raw", "farmers", "web", "import"])
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--lang", default=None, choices=["中文", "英文"])
    ap.add_argument("--tag", default="")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--provider", default=None, help="同时指定翻译+分析模型")
    ap.add_argument("--tr-provider", default=None, help="只指定翻译模型")
    ap.add_argument("--az-provider", default=None, help="只指定分析模型")
    ap.add_argument("--config", default=None, help="额外配置文件路径")
    # --- 联网检索 ---
    ap.add_argument("--query", default=None, help="联网检索式（给了就走 web 源）")
    ap.add_argument("--web-sources", default="openalex",
                    help="逗号分隔：openalex,arxiv,crossref,s2")
    ap.add_argument("--oa-only", action="store_true", help="只检索开放获取")
    ap.add_argument("--year-from", type=int, default=None)
    ap.add_argument("--year-to", type=int, default=None)
    ap.add_argument("--sort", default="relevance",
                    choices=["relevance", "cited", "date"])
    ap.add_argument("--min-relevance", type=int, default=2,
                    help="标题+摘要至少命中几个检索关键词，0=不过滤（默认 2）")
    ap.add_argument("--fulltext-only", action="store_true",
                    help="丢弃只有摘要的条目（默认保留并做摘要级分析）")
    # --- 题录导入（知网/维普无公开 API，走导出文件）---
    ap.add_argument("--import-file", default=None,
                    help="知网/维普导出的题录文件（RefWorks/BibTeX/EndNote）")
    ap.add_argument("--import-format", default=None,
                    choices=["refworks", "bibtex", "endnote"])
    a = ap.parse_args()
    if a.config:
        CONFIG.update(load_config(extra=a.config))
    srcs = tuple(s.strip() for s in (a.web_sources or "").split(",") if s.strip())
    run(source=a.source, limit=a.limit, lang=a.lang, tag=a.tag,
        max_pages=a.max_pages, out=a.out,
        tr_pid=a.tr_provider or a.provider,
        az_pid=a.az_provider or a.provider,
        query=a.query, web_sources=srcs or ("openalex",),
        import_file=a.import_file, oa_only=a.oa_only,
        year_from=a.year_from, year_to=a.year_to, web_sort=a.sort,
        abstract_only_ok=not a.fulltext_only,
        min_relevance=a.min_relevance)
