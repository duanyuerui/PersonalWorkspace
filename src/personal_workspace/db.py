"""SQLite 数据层：目录解析、连接、建表迁移、备份恢复。

约定（见 PRD 5.5 / 9.x）：
- 时刻字段一律存 INTEGER epoch 秒（UTC），展示时转本地时区；
- 纯日期字段（planned_date/due_date/journal_date）存 TEXT 'YYYY-MM-DD'，
  按本地日期语义使用，不做时区换算；
- 业务数据软删除（is_deleted），完成历史永不覆盖。
"""
import datetime
import os
import sqlite3
import sys
import time

SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sys_migration (
  version    INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sys_category (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,
  color      TEXT NOT NULL DEFAULT '',
  sort       INTEGER NOT NULL DEFAULT 0,
  disabled   INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sys_tag (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  name      TEXT NOT NULL UNIQUE,
  color_idx INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sys_item_tag (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  item_type TEXT NOT NULL,
  item_id   INTEGER NOT NULL,
  tag_id    INTEGER NOT NULL REFERENCES sys_tag(id) ON DELETE CASCADE,
  UNIQUE(item_type, item_id, tag_id)
);
CREATE TABLE IF NOT EXISTS biz_task (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  title             TEXT NOT NULL,
  description       TEXT NOT NULL DEFAULT '',
  status            INTEGER NOT NULL DEFAULT 0,   -- 0未开始 1进行中 2已完成 3已取消
  priority          INTEGER NOT NULL DEFAULT 1,   -- 0低 1普通 2高 3紧急
  category_id       INTEGER REFERENCES sys_category(id) ON DELETE SET NULL,
  planned_date      TEXT,                          -- 'YYYY-MM-DD'
  due_date          TEXT,                          -- 'YYYY-MM-DD'
  due_time          TEXT,                          -- 'HH:MM' 可空
  estimated_minutes INTEGER,
  completed_at      INTEGER,                       -- epoch UTC
  completed_via     TEXT,                          -- 完成来源：手动 / 日报
  created_via       TEXT,                          -- 创建来源：手动 / 明日计划 / 收集箱 / 日报
  recurrence_id     INTEGER,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL,
  is_deleted        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_task_planned  ON biz_task(planned_date, is_deleted);
CREATE INDEX IF NOT EXISTS idx_task_due      ON biz_task(due_date, is_deleted);
CREATE INDEX IF NOT EXISTS idx_task_status   ON biz_task(status, is_deleted);
CREATE INDEX IF NOT EXISTS idx_task_cat      ON biz_task(category_id);
CREATE INDEX IF NOT EXISTS idx_task_completed ON biz_task(completed_at);
CREATE TABLE IF NOT EXISTS biz_task_history (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    INTEGER NOT NULL REFERENCES biz_task(id) ON DELETE CASCADE,
  action     TEXT NOT NULL,          -- created / status / edit
  detail     TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_history ON biz_task_history(task_id, created_at);
CREATE TABLE IF NOT EXISTS biz_calendar_event (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  start_at    INTEGER NOT NULL,      -- epoch UTC
  end_at      INTEGER NOT NULL,
  is_all_day  INTEGER NOT NULL DEFAULT 0,
  category_id INTEGER REFERENCES sys_category(id) ON DELETE SET NULL,
  task_id     INTEGER REFERENCES biz_task(id) ON DELETE SET NULL,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  is_deleted  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_event_start ON biz_calendar_event(start_at, is_deleted);
CREATE INDEX IF NOT EXISTS idx_event_cat   ON biz_calendar_event(category_id);
CREATE TABLE IF NOT EXISTS biz_reminder (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  title             TEXT NOT NULL,
  trigger_at        INTEGER NOT NULL,   -- epoch UTC，已含提前量
  source_type       TEXT,               -- 'task' | 'event' | NULL
  source_id         INTEGER,
  status            INTEGER NOT NULL DEFAULT 0,  -- 0待触发 1已触发 2已处理 3已忽略
  snoozed_until     INTEGER,
  last_triggered_at INTEGER,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminder_trigger ON biz_reminder(status, trigger_at);
CREATE TABLE IF NOT EXISTS biz_journal (
  journal_date TEXT PRIMARY KEY,       -- 'YYYY-MM-DD'
  content      TEXT NOT NULL DEFAULT '',            -- 今日总结（手动）
  plan         TEXT NOT NULL DEFAULT '',            -- 明日计划（换行分隔）
  plan_state   TEXT NOT NULL DEFAULT '{}',          -- {行号: 'done'|'dismissed'}
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS biz_inbox (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT NOT NULL,
  note       TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  is_deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS biz_special_day (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  type       TEXT NOT NULL DEFAULT '自定义',  -- 生日 / 纪念日 / 自定义
  date       TEXT NOT NULL,                   -- recur=1: 'MM-DD'；否则 'YYYY-MM-DD'
  recur      INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sys_setting (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);
"""

_DEFAULT_SETTINGS = {
    "start_page": "today",       # 启动页面
    "default_lead": "30",        # 默认提前提醒（分钟）
    "desktop_notify": "1",       # 桌面通知开关（浏览器 Notification + 后台 Toast）
    "keep_alive": "0",           # 后台驻留：关闭窗口后服务继续运行
}

_DEFAULT_CATEGORIES = [("工作", "cat-work"), ("生活", "cat-life"), ("学习", "cat-study")]


def data_dir():
    """本地数据目录：%LOCALAPPDATA%/PersonalWorkspace（PWS_DATA_DIR 可覆盖）。"""
    override = os.environ.get("PWS_DATA_DIR")
    if override:
        root = os.path.abspath(os.path.expanduser(override))
    else:
        home = os.path.expanduser("~")
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or os.path.join(
                home, "AppData", "Local")
            root = os.path.join(base, "PersonalWorkspace")
        elif sys.platform == "darwin":
            root = os.path.join(home, "Library", "Application Support",
                                "PersonalWorkspace")
        else:
            base = (os.environ.get("XDG_DATA_HOME")
                    or os.path.join(home, ".local", "share"))
            root = os.path.join(base, "personal-workspace")
    return root


def db_path():
    return os.path.join(data_dir(), "workspace.db")


def backups_dir():
    return os.path.join(data_dir(), "Backups")


def connect():
    """每个请求一个连接：忙等 + 外键。写入用 write_txn() 串行化。"""
    os.makedirs(data_dir(), exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class write_txn:
    """写事务：BEGIN IMMEDIATE 串行化多线程写入，异常回滚。

    用法：
        with write_txn(conn):
            conn.execute(...)
    """

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        return False


def _ensure_column(conn, table, column, decl):
    """旧库升级：缺列时补上（幂等）。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    if column not in cols:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
        return True
    return False


def init_db():
    """建库建表；预置默认分类与设置。幂等，可在每次启动时调用。"""
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        now = int(time.time())
        # executescript 自带隐式 COMMIT，必须放在事务外
        conn.executescript(_SCHEMA)
        _ensure_column(conn, "biz_task", "completed_via", "TEXT")
        _ensure_column(conn, "biz_task", "created_via", "TEXT")
        row = conn.execute(
            "SELECT version FROM sys_migration ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            with write_txn(conn):
                for name, color in _DEFAULT_CATEGORIES:
                    conn.execute(
                        "INSERT OR IGNORE INTO sys_category"
                        " (name, color, sort, created_at, updated_at)"
                        " VALUES (?,?,0,?,?)", (name, color, now, now))
                for key, value in _DEFAULT_SETTINGS.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO sys_setting(key, value)"
                        " VALUES (?,?)", (key, value))
                conn.execute(
                    "INSERT INTO sys_migration(version, applied_at)"
                    " VALUES (?,?)", (SCHEMA_VERSION, now))
        else:
            version = row["version"]
            if version < 2:
                # v2：补完成来源；历史记录里由「每日总结」产生/勾掉的记为日报
                with write_txn(conn):
                    conn.execute(
                        "UPDATE biz_task SET completed_via='日报'"
                        " WHERE status=2 AND completed_via IS NULL AND EXISTS ("
                        "   SELECT 1 FROM biz_task_history h"
                        "   WHERE h.task_id=biz_task.id"
                        "     AND h.action IN ('created','status')"
                        "     AND h.detail LIKE '%每日总结%')")
                    conn.execute(
                        "UPDATE biz_task SET completed_via='手动'"
                        " WHERE status=2 AND completed_via IS NULL")
                    conn.execute(
                        "INSERT OR IGNORE INTO sys_migration(version, applied_at)"
                        " VALUES (?,?)", (2, now))
            if version < 3:
                # v3：补创建来源；历史记录里由「明日计划/收集箱/每日总结」产生的任务
                with write_txn(conn):
                    for via, like in (("明日计划", "%明日计划%"),
                                      ("收集箱", "%收集箱%"),
                                      ("日报", "%每日总结%")):
                        conn.execute(
                            "UPDATE biz_task SET created_via=?"
                            " WHERE created_via IS NULL AND id IN ("
                            "   SELECT task_id FROM biz_task_history"
                            "   WHERE action='created' AND detail LIKE ?)",
                            (via, like))
                    conn.execute(
                        "INSERT OR IGNORE INTO sys_migration(version, applied_at)"
                        " VALUES (?,?)", (3, now))
            if version < 4:
                # v4：往日日报补记的完成记录，完成时间归到日报日期，
                # 不再算进「今天」的完成统计（created_via='日报' 才可能来自补记）
                with write_txn(conn):
                    rows = conn.execute(
                        "SELECT id, planned_date, completed_at FROM biz_task"
                        " WHERE status=2 AND is_deleted=0"
                        " AND created_via='日报' AND completed_via='日报'"
                        " AND planned_date IS NOT NULL AND planned_date <> ''"
                        " AND completed_at IS NOT NULL").fetchall()
                    for r in rows:
                        if time.strftime("%Y-%m-%d", time.localtime(
                                r["completed_at"])) == r["planned_date"]:
                            continue
                        try:
                            noon = int(time.mktime(
                                datetime.datetime.strptime(
                                    r["planned_date"], "%Y-%m-%d")
                                .replace(hour=12, minute=0, second=0)
                                .timetuple()))
                        except ValueError:
                            continue
                        conn.execute(
                            "UPDATE biz_task SET completed_at=? WHERE id=?",
                            (noon, r["id"]))
                    conn.execute(
                        "INSERT OR IGNORE INTO sys_migration(version, applied_at)"
                        " VALUES (?,?)", (4, now))
    finally:
        conn.close()


def backup_now(tag=""):
    """用 SQLite Backup API 备份到 Backups/，返回备份文件路径。"""
    os.makedirs(backups_dir(), exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "workspace-%s%s.db" % (tag + "-" if tag else "", stamp)
    dest_path = os.path.join(backups_dir(), name)
    src = connect()
    try:
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    return dest_path


def restore_from(path):
    """从备份恢复；恢复前自动备份当前库（PRD 4.12）。返回 (备份路径, 恢复前备份)。"""
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise ValueError("备份文件不存在: %s" % path)
    pre = backup_now(tag="before-restore")
    src = sqlite3.connect("file:///" + path.replace("\\", "/") + "?mode=ro",
                          uri=True)
    try:
        dst = connect()
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return path, pre


def list_backups():
    """列出备份文件，新的在前。"""
    d = backups_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for name in os.listdir(d):
        if not name.endswith(".db"):
            continue
        p = os.path.join(d, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": name, "path": p, "size": st.st_size,
                    "mtime": int(st.st_mtime)})
    out.sort(key=lambda x: -x["mtime"])
    return out
