# -*- coding: utf-8 -*-
"""config.py —— 服务器级配置（全部来自环境变量，绝不硬编码密钥）。

⚠️ 安全模型（v3）：
  · 服务器自身凭证（WX_APPID/WX_SECRET）只从环境变量读取，不再支持把
    AppSecret 写进任何随包文件；wx_secret.json 仅为历史兼容且 git 忽略。
  · 用户 API Key 一律经 keyvault 加密后落 SQLite（主密钥来自
    LENS_MASTER_KEY），磁盘上永远不出现明文 Key。
  · 蜜罐：内置若干「以假乱真」的假 Key。攻击者若把它配进系统或从诱饵
    端点取走使用，会立刻被记录（IP/openid/时间入库 + 控制台告警），
    而真实业务毫无损失。

部署时必须在云托管控制台配置：
  LENS_MASTER_KEY   32+ 位随机 hex（openssl rand -hex 32）—— 主加密密钥
  WX_APPID / WX_SECRET / WX_ENV           —— 仅公网通道 code2Session 需要
可选：
  LENS_DATA_DIR      持久卷挂载点（默认 ./data，生产务必指向 CFS）
  LENS_DB_PATH       SQLite 路径（默认 <data>/lens.db）
  LENS_LLM_CONCURRENCY  LLM 并发上限（默认 8；防止单实例打爆上游限频）
  LENS_RATE_LIMIT    每用户每分钟写请求数上限（默认 120）
  LENS_INSTANCE_ID   实例标识（多实例部署时区分日志来源）
"""
import os
import secrets as _secrets

# ---------------- 基础路径 ----------------
DATA_DIR = os.path.abspath(os.environ.get("LENS_DATA_DIR")
                           or os.environ.get("LENS_DATA_ROOT")
                           or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
DB_PATH = os.path.abspath(os.environ.get("LENS_DB_PATH") or os.path.join(DATA_DIR, "lens.db"))
MASTER_KEY_FILE = os.path.join(DATA_DIR, "master.key")
INSTANCE_ID = os.environ.get("LENS_INSTANCE_ID") or "i0"

# ---------------- 并发 / 限流 ----------------
LLM_CONCURRENCY = max(1, int(os.environ.get("LENS_LLM_CONCURRENCY") or 8))
RATE_LIMIT_PER_MIN = max(10, int(os.environ.get("LENS_RATE_LIMIT") or 120))
DB_BUSY_TIMEOUT_MS = int(os.environ.get("LENS_DB_BUSY_TIMEOUT_MS") or 5000)

# ---------------- 主密钥（加密用户 Key 用） ----------------
_MASTER = (os.environ.get("LENS_MASTER_KEY") or "").strip()


def _autogen_master():
    """env 未配主密钥时的兜底：生成一次并写入数据目录（仅本地开发用）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.isfile(MASTER_KEY_FILE):
        try:
            k = open(MASTER_KEY_FILE, "r", encoding="utf-8").read().strip()
            if len(k) >= 32:
                return k
        except Exception:
            pass
    k = _secrets.token_hex(32)
    try:
        with open(MASTER_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(k)
        try:
            os.chmod(MASTER_KEY_FILE, 0o600)
        except Exception:
            pass
    except Exception:
        pass
    return k


if not _MASTER:
    _MASTER = _autogen_master()
    print(f"[config][warn] 未设置 LENS_MASTER_KEY，已自动生成并落盘 {MASTER_KEY_FILE}。"
          f"生产环境请在云托管控制台配置环境变量，避免依赖本机文件。")
MASTER_KEY = _MASTER.encode("utf-8")

# ---------------- 微信凭证（仅环境变量） ----------------
WX_APPID = (os.environ.get("WX_APPID") or "").strip()
WX_SECRET = (os.environ.get("WX_SECRET") or "").strip()
WX_ENV = (os.environ.get("WX_ENV") or "").strip()

# ---------------- 蜜罐 ----------------
# 这些 Key 看起来完全像真实凭证，但指向不存在/无效的账户。
# 触发路径：① 攻击者从诱饵端点 /api/admin/config 抓走后回填使用；
#          ② 内鬼/越权者把偷来的「key」粘进设置页。
# 任何一次命中都会：入库 honeypot 表（IP/openid/时间）+ 控制台 ALERT。
# ⚠️ 该列表绝不发给前端、绝不出现在 /api/* 的正常响应里。
HONEYPOT_KEYS = frozenset({
    "sk-prod-9f14e45fceea167a5a36dedd4bea2543f88a5c7e1aa0b2e4d6f8a1c3e5b7d9f1",
    "sk-backup-4a7d1ed414474e4033ac29ccb8653d9b5c7e1aa0b2e4d6f8a1c3e5b7d9f1a",
    "sk-lens-internal-77e4d1c4e2f5a6b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8",
    "sk-deepseek-archive-3b5d2f7a9c1e8b4d6f0a2c8e4b6d8f0a2c4e6b8d0f2a4c6e8b0d",
    "sk-glm-reserve-5e9c1a7b3d5f7a9b1c3d5e7f9a1b3c5d7e9f1a3b5c7d9e1f3a5b7c9d1",
})

# 诱饵端点返回的「高价值配置」（全是假 Key，专供攻击者搬运）
DECOY_CONFIG = {
    "note": "internal use only",
    "active_provider": "prod-primary",
    "providers": [
        {"id": "prod-primary", "name": "生产-主力翻译", "protocol": "openai",
         "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
         "api_key": "sk-prod-9f14e45fceea167a5a36dedd4bea2543f88a5c7e1aa0b2e4d6f8a1c3e5b7d9f1"},
        {"id": "prod-reasoning", "name": "生产-科研助手", "protocol": "openai",
         "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4.6",
         "api_key": "sk-glm-reserve-5e9c1a7b3d5f7a9b1c3d5e7f9a1b3c5d7e9f1a3b5c7d9e1f3a5b7c9d1"},
        {"id": "prod-backup", "name": "生产-备用线路", "protocol": "openai",
         "base_url": "https://api.deepseek.com/v1", "model": "deepseek-reasoner",
         "api_key": "sk-backup-4a7d1ed414474e4033ac29ccb8653d9b5c7e1aa0b2e4d6f8a1c3e5b7d9f1a"},
    ],
}


def honeypot_hit(kind, ip="", openid="", detail=""):
    """记录一次蜜罐触发：入库 + 控制台告警。绝不抛异常。"""
    try:
        import time
        import storage as _st
        _st.log_honeypot(kind=kind, ip=ip, openid=openid, detail=detail)
        print(f"[HONEYPOT][ALERT] kind={kind} ip={ip} openid={openid} detail={detail[:120]}",
              flush=True)
    except Exception as e:
        print("[HONEYPOT] 记录失败:", e, flush=True)
