# -*- coding: utf-8 -*-
"""keyvault.py —— 用户 API Key 的加密保险库。

威胁模型：攻击者拿到服务器磁盘/持久卷快照（或云端误泄漏的存储桶）。
  · 旧方案：users/<oid>/config.user.json 里是**明文 Key** —— 一锅端。
  · 本方案：Key 配置整体加密成 blob 入 SQLite，主密钥只在环境变量
    （LENS_MASTER_KEY）里，磁盘上既无明文也无可解密材料。
加密：SHA-256 计数器流 + HMAC-SHA256 完整性标签（零第三方依赖）。
  blob = b"LV1" || nonce(16) || ciphertext || tag(32)
蜜罐：提交/使用 config.HONEYPOT_KEYS 中的假 Key 会立即告警入库。
"""
import hashlib
import hmac
import json
import os
import secrets

import config as _cfg
import storage as _st

_MAGIC = b"LV1"
_NONCE_LEN = 16

# 匿名/公网域名访问（无微信 openid）时使用的稳定哨兵身份。
# storage 的 user_config 表对空 oid 直接返回 None/False，因此必须用固定哨兵串，
# 否则「公网网页保存 Key」会静默失败（这正是「云端收不到 Key」的链路之一）。
ANON = "__lens_anon__"


def _norm_oid(oid) -> str:
    return (oid or "").strip() or ANON


# ---------------- 流加密原语 ----------------
def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(out[:n])


def encrypt(plaintext: bytes) -> bytes:
    nonce = secrets.token_bytes(_NONCE_LEN)
    ks = _keystream(_cfg.MASTER_KEY, nonce, len(plaintext))
    ct = bytes(a ^ b for a, b in zip(plaintext, ks))
    tag = hmac.new(_cfg.MASTER_KEY, _MAGIC + nonce + ct, hashlib.sha256).digest()
    return _MAGIC + nonce + ct + tag


def decrypt(blob: bytes):
    """解密；完整性校验失败返回 None（绝不返回被篡改的数据）。

    依次尝试所有主密钥（_cfg.MASTER_KEYS），以支持主密钥轮换：
    旧密钥期间加密的 blob，在更换新密钥后依然能解密。
    """
    try:
        if not blob or blob[:3] != _MAGIC or len(blob) < 3 + _NONCE_LEN + 32:
            return None
        nonce = blob[3:3 + _NONCE_LEN]
        ct = blob[3 + _NONCE_LEN:-32]
        tag = blob[-32:]
        for key in _cfg.MASTER_KEYS:
            want = hmac.new(key, _MAGIC + nonce + ct, hashlib.sha256).digest()
            if hmac.compare_digest(tag, want):
                ks = _keystream(key, nonce, len(ct))
                return bytes(a ^ b for a, b in zip(ct, ks))
        return None
    except Exception:
        return None


# ---------------- 用户配置的存取 ----------------
def save_user_config(oid, cfg: dict) -> bool:
    """把某用户的完整 provider 配置加密入库。

    返回 True 仅当：写入成功 **且** 立即回读能正常解密 —— 否则视为“假保存”
    （典型：主密钥不匹配），绝不向调用方谎报成功（过去这会导致“云端收不到 Key”）。
    内存/DB/日志全程无明文。
    """
    if not isinstance(cfg, dict):
        return False
    oid = _norm_oid(oid)
    try:
        blob = encrypt(json.dumps(cfg, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return False
    _st.ensure_user(oid)
    if not _st.put_user_config_blob(oid, blob):
        return False
    # 回读校验：确认落盘后能被正常解密
    raw = load_user_config(oid)
    if raw is None:
        print(f"[keyvault][warn] 用户 {oid[:8]}*** 的 Key 已写入但回读解密失败"
              f"（主密钥不匹配？），保存未真正生效", flush=True)
        return False
    return True


def load_user_config(oid):
    """取出并解密某用户的 provider 配置；无/损坏 → None。"""
    oid = _norm_oid(oid)
    blob = _st.get_user_config_blob(oid)
    if not blob:
        return None
    raw = decrypt(blob)
    if raw is None:
        # 被篡改或主密钥更换 —— 视为无配置，绝不带病运行
        print(f"[keyvault][warn] 用户 {oid[:8]}*** 的配置解密失败（主密钥不匹配或数据被改）",
              flush=True)
        return None
    import json
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


# ---------------- 蜜罐检测 ----------------
def check_honeypot(api_key, ip="", openid="") -> bool:
    """提交的 Key 命中蜜罐 → 记录告警并返回 True（调用方应拒绝）。"""
    k = str(api_key or "").strip()
    if k and k in _cfg.HONEYPOT_KEYS:
        _cfg.honeypot_hit(kind="key_submit", ip=ip, openid=openid,
                          detail=f"提交了蜜罐 Key {k[:18]}***")
        return True
    return False


def scan_config_honeypot(cfg: dict, ip="", openid="") -> bool:
    """整份配置里混入蜜罐 Key 也拦（防拆分提交绕过）。"""
    try:
        for p in (cfg.get("providers") or []):
            if check_honeypot((p or {}).get("api_key"), ip=ip, openid=openid):
                return True
    except Exception:
        pass
    return False


# ---------------- 脱敏展示 ----------------
def mask_key(api_key: str) -> str:
    k = str(api_key or "")
    if len(k) <= 10:
        return "***"
    return k[:6] + "***" + k[-4:]
