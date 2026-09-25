# -*- coding: utf-8 -*-
"""
结构化翻译引擎（云端 API 版）
区别于 LiyuAgent 的本地翻译器：
  1) 只走云端 API，禁用本地模型
  2) 保护位 + 段落严格对齐（编号回填，不靠模型自觉）
  3) 长文分批判走，单批失败自动降级重试
  4) 输出结构化块，供网页渲染中英对照

模型无关：所有结构化输出都经 coerce_list / parse_json_loose 容错，
          不假设模型一定按「纯数组」或「纯对象」返回。
"""
import re

from llm_client import coerce_list

# ---- 保护位：公式 / DOI / 引用 / 图表号 / 单位 / URL ----
PROTECT = [
    (re.compile(r"\$\$.+?\$\$", re.S), "MATH"),
    (re.compile(r"\$[^$\n]{1,200}\$"), "MATH"),
    (re.compile(r"\\\((.+?)\\\)", re.S), "MATH"),
    (re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+"), "DOI"),
    (re.compile(r"https?://\S+"), "URL"),
    (re.compile(r"\[\d+(?:\s*[,\-–]\s*\d+)*\]"), "CITE"),
    (re.compile(r"\((?:[A-Z][A-Za-z\-]+(?:\s+et\s+al\.?)?,?\s*\d{4}[a-z]?[;,\s]*)+\)"), "CITE"),
    (re.compile(r"\b(?:Fig(?:ure)?|Table|Eq(?:uation)?|Section|Sec\.|Alg(?:orithm)?)\s*\.?\s*\d+[a-z]?", re.I), "REF"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|MPa|GPa|kPa|nm|μm|um|mm|cm|km|kg|mg|ms|ns|ps|fs|K|°C|℃|eV|kJ/mol|kcal/mol|Hz|GHz|m3|m³|ha|hm2)\b"), "UNIT"),
]

SYSTEM_TRANSLATE = (
    "你是资深学术翻译引擎，专门翻译科研论文。"
    "风格要求：学术书面语，术语准确统一，长句按中文习惯拆分但不丢信息，"
    "不添加任何解释、评注或总结。"
)

SYSTEM_TERMS = (
    "你是科研术语专家，负责从论文片段中抽取关键术语并给出规范中文译名。"
)


def mask(text):
    store, out, n = {}, text, 0
    for rx, tag in PROTECT:
        def rep(m):
            nonlocal n
            key = f"⟦{tag}{n}⟧"
            store[key] = m.group(0)
            n += 1
            return key
        out = rx.sub(rep, out)
    return out, store


def unmask(text, store):
    for k, v in store.items():
        text = text.replace(k, v)
    return text


class Translator:
    """结构化翻译。pid 指定用哪个 provider；不指定则按 translate 角色自动挑。"""

    def __init__(self, llm, max_chars=2600, pid=None):
        self.llm = llm
        self.max_chars = max_chars
        self.pid = pid
        self.role = "translate"
        self._cache = {}

    def _call(self, prompt, system=None, max_tokens=2400, temperature=0.2, json_mode=False):
        """统一出调用：优先 pid，其次按角色挑 provider。"""
        if self.pid:
            if json_mode:
                return self.llm.chat_json(prompt, system=system, max_tokens=max_tokens,
                                          temperature=temperature, pid=self.pid)
            return self.llm.chat(prompt, system=system, max_tokens=max_tokens,
                                 temperature=temperature, pid=self.pid)
        p = self.llm.pick_for(self.role)
        pid = p.get("id")
        if json_mode:
            return self.llm.chat_json(prompt, system=system, max_tokens=max_tokens,
                                      temperature=temperature, pid=pid)
        return self.llm.chat(prompt, system=system, max_tokens=max_tokens,
                             temperature=temperature, pid=pid)

    # ---------- 术语抽取（全文一次，供后续翻译锚定） ----------
    def extract_terms(self, sample_text, limit=40):
        prompt = (
            f"从下面论文片段中抽取最多 {limit} 个关键专业术语，"
            "每个给出规范简体中文译名。\n"
            "只输出一个 JSON 数组，不要包裹成对象，格式：\n"
            '[{"en":"术语原文","zh":"中文译名"}]\n'
            "（注意：最外层必须是方括号 [ ]，不要写成 {\"terms\": [...]}）\n"
            "要求：优先抽取领域核心概念、方法名、指标名；不要收录普通词汇；中文译名要符合该领域通行译法。\n\n"
            f"片段：\n{sample_text[:4000]}"
        )
        try:
            data = self._call(prompt, system=SYSTEM_TERMS, max_tokens=1800,
                              temperature=0.1, json_mode=True)
        except Exception:
            return []
        # 模型可能把数组包进 {"terms":[...]} → 容错转 list，避免整批术语被丢弃
        data = coerce_list(data)
        if not isinstance(data, list):
            return []
        out = []
        for d in data:
            if isinstance(d, dict) and d.get("en") and d.get("zh"):
                out.append({"en": str(d["en"]).strip(), "zh": str(d["zh"]).strip()})
        return out[:limit]

    # ---------- 批量翻译 ----------
    def translate_batch(self, blocks, terms=None, target="zh"):
        """blocks: [{"idx":int,"text":str,"kind":str}] -> {idx: 译文}

        用 ⟦SEP⟧ 分隔，模型不守约时按编号回填兜底。
        """
        if not blocks:
            return {}
        whole = {b["idx"]: b["text"] for b in blocks}
        # 命中缓存
        todo = [b for b in blocks if b["text"] not in self._cache]
        for b in blocks:
            if b["text"] in self._cache:
                whole[b["idx"]] = self._cache[b["text"]]
        hit = {b["idx"]: self._cache[b["text"]] for b in blocks if b["text"] in self._cache}

        if not todo:
            return hit

        # 拼接（带编号，便于缺段定位）
        parts, store_all = [], {}
        for b in todo:
            m, st = mask(b["text"])
            store_all.update(st)
            parts.append(f"⟦B{b['idx']}⟧\n{m}")
        joined = "\n\n".join(parts)

        term_hint = ""
        if terms:
            term_hint = "术语必须按此对照（严格执行）：" + "; ".join(
                f"{t['en']}={t['zh']}" for t in terms[:40]) + "\n"

        prompt = (
            f"把下面 {len(todo)} 个带编号的学术段落翻译成简体中文。\n"
            "硬性要求：\n"
            f"1. 必须输出 {len(todo)} 段，每段以它自己的编号标记开头（形如 ⟦B12⟧），编号从原文照抄，不得改动、不得合并、不得漏段；\n"
            "2. 只翻译正文，编号标记本身保持原样；\n"
            "3. 不增删内容，不写解释、不写总结；\n"
            "4. 学术书面语，术语统一；\n"
            "5. ⟦MATH*⟧⟦DOI*⟧⟦CITE*⟧⟦REF*⟧⟦URL*⟧⟦UNIT*⟧ 是占位符，原样保留不翻译。\n"
            + term_hint +
            f"\n原文：\n{joined}\n\n译文："
        )
        raw = self._call(
            prompt, system=SYSTEM_TRANSLATE,
            max_tokens=min(8000, 300 + sum(len(b["text"]) for b in todo)),
            temperature=0.2)
        raw = unmask(raw, store_all)
        got = self._parse_numbered(raw)

        out = dict(hit)
        for b in todo:
            txt = got.get(b["idx"])
            if txt and len(txt) > 2:
                self._cache[b["text"]] = txt
                out[b["idx"]] = txt
            else:
                out[b["idx"]] = ""     # 标记缺失，由上层决定是否重试
        return out

    @staticmethod
    def _parse_numbered(raw):
        """解析 ⟦B12⟧ 分段结果。"""
        res = {}
        if not raw:
            return res
        pattern = re.compile(r"⟦B(\d+)⟧\s*")
        ms = list(pattern.finditer(raw))
        if not ms:
            return res
        for i, m in enumerate(ms):
            idx = int(m.group(1))
            start = m.end()
            end = ms[i + 1].start() if i + 1 < len(ms) else len(raw)
            body = raw[start:end].strip()
            if body:
                res[idx] = body
        return res

    # ---------- 单段重试 ----------
    def translate_one(self, block, terms=None):
        m, st = mask(block["text"])
        term_hint = ""
        if terms:
            term_hint = "术语对照：" + "; ".join(
                f"{t['en']}={t['zh']}" for t in terms[:40]) + "\n"
        prompt = (
            "把下面学术段落翻译成简体中文，学术书面语，不增删内容，不加解释。\n"
            "⟦MATH*⟧⟦DOI*⟧⟦CITE*⟧⟦REF*⟧⟦URL*⟧⟦UNIT*⟧ 是占位符，原样保留。\n"
            + term_hint +
            f"\n原文：\n{m}\n\n译文："
        )
        try:
            out = self._call(prompt, system=SYSTEM_TRANSLATE,
                             max_tokens=2200, temperature=0.2)
        except Exception:
            return ""
        return unmask((out or "").strip(), st)

    # ---------- 全文翻译（自动分批判 + 缺段补齐） ----------
    def translate_blocks(self, blocks, terms=None, progress=None):
        """返回 {idx: 译文}，保证每个非公式块的 idx 都有键。"""
        result = {}
        batches = self._batch(blocks)
        for bi, batch in enumerate(batches, 1):
            if progress:
                progress(bi, len(batches))
            try:
                got = self.translate_batch(batch, terms=terms)
            except Exception as e:
                got = {b["idx"]: "" for b in batch}
                if progress:
                    progress(bi, len(batches), err=str(e)[:120])
            result.update(got)
        # 缺段补齐（单段重试）
        missing = [b for b in blocks if not result.get(b["idx"])]
        for b in missing:
            t = self.translate_one(b, terms=terms)
            if t:
                result[b["idx"]] = t
        return result

    def _batch(self, blocks):
        batches, cur, cur_len = [], [], 0
        for b in blocks:
            blen = len(b["text"])
            if cur and cur_len + blen > self.max_chars:
                batches.append(cur)
                cur, cur_len = [], 0
            cur.append(b)
            cur_len += blen
        if cur:
            batches.append(cur)
        return batches


def build_bilingual(blocks, trans):
    """拼装中英对照结构，供网页用。"""
    out = []
    for b in blocks:
        out.append({
            "idx": b["idx"],
            "page": b["page"],
            "kind": b["kind"],
            "source": b["text"],
            "target": trans.get(b["idx"], "") if b["kind"] != "formula" else b["text"],
            "keep_original": b["kind"] == "formula",
        })
    return out
