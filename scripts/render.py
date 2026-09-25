# -*- coding: utf-8 -*-
"""
网页渲染器：把 lens_data.json 渲染成单文件交互式网页
- 左侧：文献列表（可搜索/筛选）
- 中间：中英对照译文（可折叠、按页跳转）
- 右侧：创新点卡片（点击 → 弹出知识点面板）
- 原文分屏：页图双层 —— 内联轻量预览（秒开），灯箱按需读磁盘高清原图
- 全部数据内联，离线可用，双击即开
"""
import os
import json
import base64

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEMPLATE = os.path.join(ROOT, "assets", "template.html")

# 内联预览参数：分屏面板实际显示宽度 ~600px，460px 足够，体积只有原图 1/3
PREV_MAX_W = 460
PREV_Q = 42


def _collect_presets():
    """前端 API 对话框的预设芯片表(只读自 serve.py 的 API_PRESETS)。

    优先从 serve 导入;老版本没有该常量时,回退到 llm_models 的内置预设。
    """
    try:
        from serve import API_PRESETS as P
        return [{"id": p["id"], "name": p["name"],
                 "protocol": p["protocol"], "base_url": p["base_url"],
                 "model": p["model"]} for p in P]
    except Exception:
        pass
    try:
        from llm_models import PRESETS as P
        return [{"id": k, "name": v.get("name", k),
                 "protocol": v.get("protocol", "openai"),
                 "base_url": v.get("base_url", ""),
                 "model": v.get("model", "")} for k, v in P.items()]
    except Exception:
        return []


def _make_preview(fp, prev_fp):
    """生成轻量预览图（带磁盘缓存）。返回 (预览路径, 宽, 高)。"""
    from PIL import Image
    if os.path.isfile(prev_fp) \
            and os.path.getmtime(prev_fp) >= os.path.getmtime(fp):
        with Image.open(prev_fp) as im:
            return prev_fp, im.size[0], im.size[1]
    im = Image.open(fp).convert("RGB")
    w, h = im.size
    if w > PREV_MAX_W:
        im = im.resize((PREV_MAX_W, round(h * PREV_MAX_W / w)), Image.LANCZOS)
    im.save(prev_fp, "JPEG", quality=PREV_Q)
    return prev_fp, w, h


def _collect_page_images(data, out_dir):
    """页图双层打包：
    - s  ：内联 dataURI 预览（分屏面板用，秒开不白屏）
    - hi ：高清原图相对本网页的路径（灯箱用；HTML 被单独挪走时自动回退 s）
    - ar ：宽高比（页卡提前占位，避免加载时塌成一条白条）
    图片不进 lens_data.json（会让数据文件膨胀几十 MB），渲染时才内联。
    """
    from PIL import Image
    imgs, cleaned = {}, []
    for d in (data.get("docs") or []):
        d2 = dict(d)
        pim = d2.pop("pages_img", None) or {}
        m = {}
        for pno, rel in (pim or {}).items():
            fp = os.path.join(ROOT, str(rel).replace("/", os.sep))
            if not os.path.isfile(fp):
                continue
            prev_fp = os.path.join(os.path.dirname(fp),
                                   "prev_" + os.path.basename(fp))
            item = {}
            try:
                pfp, w, h = _make_preview(fp, prev_fp)
                with open(pfp, "rb") as f:
                    item["s"] = "data:image/jpeg;base64," + \
                        base64.b64encode(f.read()).decode("ascii")
                if w:
                    item["ar"] = f"{w}/{h}"
            except ImportError:
                item["s"] = "data:image/jpeg;base64," + \
                    base64.b64encode(open(fp, "rb").read()).decode("ascii")
            try:  # 高清图相对路径（灯箱用）
                item["hi"] = os.path.relpath(fp, out_dir).replace("\\", "/")
            except ValueError:
                pass
            m[str(pno)] = item
        if m and d2.get("id"):
            imgs[d2["id"]] = dict(sorted(m.items(), key=lambda x: int(x[0])))
        cleaned.append(d2)
    data["docs"] = cleaned
    return imgs


def render(data_path, out_path=None):
    data = json.load(open(data_path, encoding="utf-8"))
    tpl_path = TEMPLATE
    if not os.path.exists(tpl_path):
        raise FileNotFoundError(f"模板不存在：{tpl_path}")
    if not out_path:
        out_path = os.path.join(ROOT, "outputs", "文献透镜.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tpl = open(tpl_path, encoding="utf-8").read()
    page_imgs = _collect_page_images(data, os.path.dirname(os.path.abspath(out_path)))
    payload = json.dumps(data, ensure_ascii=False)
    html_out = tpl.replace("/*__LENS_DATA__*/", payload)
    html_out = html_out.replace("__PAGE_IMGS_JSON__",
                                json.dumps(page_imgs))
    html_out = html_out.replace("__GEN_TIME__", data.get("generated_at", ""))
    html_out = html_out.replace("__PRESETS_JSON__",
                                json.dumps(_collect_presets(), ensure_ascii=False))
    # 模型名：兼容新旧数据（新数据有 models 双模型字段）
    models = data.get("models") or {}
    if models:
        mname = f"翻译 {models.get('translate','')} · 分析 {models.get('analyze','')}"
    else:
        mname = data.get("model", "")
    html_out = html_out.replace("__MODEL_NAME__", mname)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_out)
    size = os.path.getsize(out_path) / 1024 / 1024
    print(f"[+] 网页已生成：{out_path}  ({size:.2f} MB)")
    return out_path


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    render(a.data, a.out)
