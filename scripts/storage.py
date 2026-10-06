# -*- coding: utf-8 -*-
"""storage.py —— SQLite(WAL) 数据层：替代 JSON 文件存储，面向高并发与多实例。

设计要点：
  · WAL（写前日志）模式：读不阻塞写、写不阻塞读，多线程并发安全。
  · 每线程独立连接（threading.local），busy_timeout 兜底锁竞争。
  · 单文件 DB 放在持久卷（LENS_DATA_DIR → CFS），任务表/用户配置/文献库
    全部入库后，多实例部署时任何实例都能读到同一份状态（分布部署基础）。
  · 文献 doc 的完整内容以 JSON 文本存 data_json 列 —— 保留原灵活结构，
    应用层零改动；库内不再有 users/<oid>/lens_data.json 这类明文 Key。
  · 首次访问某用户时，若发现旧版 lens_data.json 则自动迁移进 DB（幂等）。

表：
  users(openid PK, created_at)
  docs(id PK, openid, title, data_json, updated_at)        —— 文献库
  user_config(openid PK, enc_blob, updated_at)             —— 加密后的 Key 配置
  uploads(id PK, openid, filename, path, size, created_at) —— 上传登记
  tasks(id PK, openid, status, stage, payload_json, updated_at) —— 跨实例任务
  honeypot(ts, kind, ip, openid, detail)                   —— 蜜罐告警
"""
import json
import os
import sqlite3
import threading
import time

import config as _cfg

_local = threading.local()
_init_lock = threading.Lock()
_inited = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  openid     TEXT PRIMARY KEY,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS docs(
  id        TEXT NOT NULL,
  openid    TEXT NOT NULL,
  title     TEXT DEFAULT '',
  data_json TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(openid, id)
);
CREATE INDEX IF NOT EXISTS idx_docs_openid ON docs(openid);
CREATE TABLE IF NOT EXISTS user_config(
  openid     TEXT PRIMARY KEY,
  enc_blob   BLOB NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads(
  id         TEXT PRIMARY KEY,
  openid     TEXT NOT NULL,
  filename   TEXT DEFAULT '',
  path       TEXT DEFAULT '',
  size       INTEGER DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_openid ON uploads(openid);
CREATE TABLE IF NOT EXISTS tasks(
  id           TEXT PRIMARY KEY,
  openid       TEXT DEFAULT '',
  status       TEXT DEFAULT 'queued',
  stage        TEXT DEFAULT '',
  payload_json TEXT DEFAULT '{}',
  updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS honeypot(
  ts     REAL NOT NULL,
  kind   TEXT DEFAULT '',
  ip     TEXT DEFAULT '',
  openid TEXT DEFAULT '',
  detail TEXT DEFAULT ''
);
"""


def _conn():
    """取当前线程的 SQLite 连接（无则新建并设 PRAGMA）。"""
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(_cfg.DB_PATH) or ".", exist_ok=True)
        c = sqlite3.connect(_cfg.DB_PATH, timeout=_cfg.DB_BUSY_TIMEOUT_MS / 1000.0,
                            check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute(f"PRAGMA busy_timeout={_cfg.DB_BUSY_TIMEOUT_MS}")
        c.execute("PRAGMA journal_mode=WAL")       # 并发读写关键
        c.execute("PRAGMA synchronous=NORMAL")     # WAL 下安全且快
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def init_db():
    global _inited
    with _init_lock:
        if _inited:
            return
        _conn().executescript(SCHEMA)
        _conn().commit()
        _inited = True
        print(f"[storage] SQLite 就绪: {_cfg.DB_PATH} (WAL)", flush=True)


def _ready():
    if not _inited:
        init_db()
    return _conn()


# ---------------- users ----------------
def ensure_user(oid):
    if not oid:
        return
    _ready().execute("INSERT OR IGNORE INTO users(openid, created_at) VALUES(?,?)",
                     (oid, time.time()))
    _ready().commit()


# ---------------- docs（文献库） ----------------
def upsert_doc(oid, doc):
    """写入/更新一篇文献（整篇 JSON 存 data_json，应用层结构不变）。"""
    if not oid or not isinstance(doc, dict) or not doc.get("id"):
        return False
    did = str(doc["id"])
    title = str(((doc.get("meta") or {}).get("title") or "")[:300])
    dj = json.dumps(doc, ensure_ascii=False)
    c = _ready()
    c.execute("""INSERT INTO docs(id,openid,title,data_json,updated_at)
                 VALUES(?,?,?,?,?)
                 ON CONFLICT(openid,id) DO UPDATE SET
                   title=excluded.title, data_json=excluded.data_json,
                   updated_at=excluded.updated_at""",
              (did, oid, title, dj, time.time()))
    c.commit()
    return True


def delete_doc(oid, did):
    c = _ready()
    c.execute("DELETE FROM docs WHERE openid=? AND id=?", (oid, str(did)))
    c.commit()
    return c.total_changes > 0


def list_docs(oid):
    """返回该用户全部 docs（应用层 dict 列表，含完整数据）。"""
    if not oid:
        return []
    rows = _ready().execute(
        "SELECT data_json FROM docs WHERE openid=? ORDER BY updated_at DESC", (oid,)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["data_json"]))
        except Exception:
            continue
    return out


def count_docs(oid):
    if not oid:
        return 0
    return _ready().execute("SELECT COUNT(*) n FROM docs WHERE openid=?",
                            (oid,)).fetchone()["n"]


def migrate_user_json(oid, json_path):
    """旧版 lens_data.json → DB 一次性迁移（幂等：DB 已有该用户文献则跳过）。"""
    if not oid or not os.path.isfile(json_path):
        return 0
    if count_docs(oid) > 0:
        return 0
    try:
        raw = json.load(open(json_path, encoding="utf-8"))
        n = 0
        for d in (raw.get("docs") or []):
            if isinstance(d, dict) and d.get("id"):
                upsert_doc(oid, d)
                n += 1
        if n:
            print(f"[storage] 已迁移用户 {oid[:8]}*** 的 {n} 篇文献 JSON→SQLite", flush=True)
        return n
    except Exception as e:
        print("[storage] JSON 迁移失败:", e, flush=True)
        return 0


# ---------------- user_config（加密的 Key 配置） ----------------
def get_user_config_blob(oid):
    if not oid:
        return None
    row = _ready().execute("SELECT enc_blob FROM user_config WHERE openid=?",
                           (oid,)).fetchone()
    return bytes(row["enc_blob"]) if row else None


def put_user_config_blob(oid, enc_blob):
    if not oid or not enc_blob:
        return False
    c = _ready()
    c.execute("""INSERT INTO user_config(openid,enc_blob,updated_at) VALUES(?,?,?)
                 ON CONFLICT(openid) DO UPDATE SET
                   enc_blob=excluded.enc_blob, updated_at=excluded.updated_at""",
              (oid, sqlite3.Binary(enc_blob), time.time()))
    c.commit()
    return True


def delete_user_config(oid):
    c = _ready()
    c.execute("DELETE FROM user_config WHERE openid=?", (oid,))
    c.commit()


# ---------------- uploads（登记，文件本体仍在磁盘） ----------------
def register_upload(oid, upid, filename, path, size=0):
    if not oid or not upid:
        return
    c = _ready()
    c.execute("""INSERT OR REPLACE INTO uploads(id,openid,filename,path,size,created_at)
                 VALUES(?,?,?,?,?,?)""", (str(upid), oid, filename or "", path or "",
                                          int(size or 0), time.time()))
    c.commit()


def get_upload(oid, upid):
    if not oid or not upid:
        return None
    row = _ready().execute("SELECT * FROM uploads WHERE openid=? AND id=?",
                           (oid, str(upid))).fetchone()
    return dict(row) if row else None


# ---------------- tasks（跨实例任务表） ----------------
def upsert_task(task_id, openid, status, stage, payload):
    c = _ready()
    c.execute("""INSERT INTO tasks(id,openid,status,stage,payload_json,updated_at)
                 VALUES(?,?,?,?,?,?)
                 ON CONFLICT(id) DO UPDATE SET
                   openid=excluded.openid, status=excluded.status,
                   stage=excluded.stage, payload_json=excluded.payload_json,
                   updated_at=excluded.updated_at""",
              (str(task_id), openid or "", status or "queued", stage or "",
               json.dumps(payload or {}, ensure_ascii=False), time.time()))
    c.commit()


def get_task(task_id):
    row = _ready().execute("SELECT * FROM tasks WHERE id=?", (str(task_id),)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
    except Exception:
        d["payload"] = {}
    return d


def load_all_tasks():
    """启动时把任务记录读回内存缓存（老格式兼容：拍平 payload 进 dict）。"""
    rows = _ready().execute("SELECT * FROM tasks").fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        try:
            payload = json.loads(d.pop("payload_json") or "{}")
        except Exception:
            payload = {}
        d.update(payload)
        out[d["id"]] = d
    return out


# ---------------- honeypot ----------------
def log_honeypot(kind, ip="", openid="", detail=""):
    c = _ready()
    c.execute("INSERT INTO honeypot(ts,kind,ip,openid,detail) VALUES(?,?,?,?,?)",
              (time.time(), kind or "", ip or "", openid or "", detail or ""))
    c.commit()


def honeypot_count():
    return _ready().execute("SELECT COUNT(*) n FROM honeypot").fetchone()["n"]


# ---------------- 健康检查 ----------------
def db_health():
    try:
        r = _ready().execute(
            "SELECT (SELECT COUNT(*) FROM docs) docs,(SELECT COUNT(*) FROM tasks) tasks,"
            "(SELECT COUNT(*) FROM honeypot) honey").fetchone()
        return {"db": "sqlite-wal", "path": _cfg.DB_PATH, "docs": r["docs"],
                "tasks": r["tasks"], "honeypot_hits": r["honey"]}
    except Exception as e:
        return {"db": "error", "error": str(e)[:120]}
