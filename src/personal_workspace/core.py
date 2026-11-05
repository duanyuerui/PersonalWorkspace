"""业务核心：日期规则、待办/日程/提醒/每日记录/收集箱/分类标签/重要日子等服务。

所有函数接收 sqlite3.Connection（db.connect() 产生），写操作用 db.write_txn。
前端展示所需的时间格式化在 webui 完成；这里只输出原始字段 + 少量计算字段
（overdue、分类快照等）。

命令行（python -m personal_workspace）：
    today | list | backup | stats
"""
import datetime as _dt
import difflib
import json
import re
import sys
import time

from . import db

# ---------------------------------------------------------------- 基础工具


def setup_stdio():
    """Windows 控制台 GBK 兜底：流一律走 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


class UserError(ValueError):
    """可安全展示给用户的错误（服务端返回 400）。"""


def now():
    return int(time.time())


def today_str():
    return time.strftime("%Y-%m-%d")


def _date(dstr):
    try:
        return _dt.datetime.strptime(dstr, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise UserError("日期格式应为 YYYY-MM-DD：%r" % (dstr,))


def date_add(dstr, days):
    return (_date(dstr) + _dt.timedelta(days=days)).strftime("%Y-%m-%d")


def date_to_epoch(dstr, hm="00:00"):
    """本地日期(+'HH:MM') → epoch 秒。"""
    try:
        return int(_dt.datetime.strptime("%s %s" % (dstr, hm),
                                         "%Y-%m-%d %H:%M").timestamp())
    except (TypeError, ValueError):
        raise UserError("时间格式应为 YYYY-MM-DD HH:MM：%r %r" % (dstr, hm))


def epoch_to_date(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""


def epoch_to_hm(ts):
    return time.strftime("%H:%M", time.localtime(ts)) if ts else ""


def check_date(dstr):
    """可空日期字段校验。"""
    if not dstr:
        return None
    return _date(dstr).strftime("%Y-%m-%d")


def check_hm(hm):
    if not hm:
        return None
    try:
        return _dt.datetime.strptime(hm, "%H:%M").strftime("%H:%M")
    except (TypeError, ValueError):
        raise UserError("时间格式应为 HH:MM：%r" % (hm,))


def strip_line_marker(s):
    """日报/明日计划的行去掉列表标记（'- '、'* '、'1. '），保留正文。"""
    s = (s or "").strip()
    if s.startswith("- ") or s.startswith("* "):
        return s[2:].strip()
    head, dot, rest = s.partition(". ")
    if dot and head.strip().isdigit():
        return rest.strip()
    return s


def _int_or(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 序列化

_STATUS = (0, 1, 2, 3)      # 未开始/进行中/已完成/已取消
_PRIORITY = (0, 1, 2, 3)    # 低/普通/高/紧急
_STATUS_NAMES = ("未开始", "进行中", "已完成", "已取消")
_PRIORITY_NAMES = ("低", "普通", "高", "紧急")


def task_row(row, tags=None, cat=None, today=None):
    t = dict(row)
    today = today or today_str()
    open_ = t["status"] in (0, 1)
    overdue = open_ and (
        (t["due_date"] and t["due_date"] < today)
        or (not t["due_date"] and t["planned_date"] and t["planned_date"] < today))
    t["status_name"] = _STATUS_NAMES[t["status"]] if t["status"] in _STATUS else ""
    t["priority_name"] = (_PRIORITY_NAMES[t["priority"]]
                          if t["priority"] in _PRIORITY else "")
    t["overdue"] = 1 if overdue else 0
    t["tags"] = tags or []
    t["cat"] = cat
    return t


def event_row(row, cat=None):
    e = dict(row)
    e["cat"] = cat
    return e


def reminder_row(row, source_title=""):
    r = dict(row)
    r["source_title"] = source_title
    return r


def _cats_map(conn):
    return {c["id"]: {"id": c["id"], "name": c["name"], "color": c["color"]}
            for c in conn.execute(
                "SELECT * FROM sys_category WHERE disabled=0")}


def _tags_for(conn, item_type, ids):
    """批量取标签：ids 为 item_id 集合，返回 {item_id: [tag...]}。"""
    out = {i: [] for i in ids}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT it.item_id, tg.id, tg.name, tg.color_idx FROM sys_item_tag it"
        " JOIN sys_tag tg ON tg.id = it.tag_id"
        " WHERE it.item_type = ? AND it.item_id IN (%s)" % marks,
        [item_type] + list(ids)).fetchall()
    for r in rows:
        out[r["item_id"]].append(
            {"id": r["id"], "name": r["name"], "color_idx": r["color_idx"]})
    return out


def _load_tasks(conn, where, args, today=None):
    rows = conn.execute(
        "SELECT * FROM biz_task WHERE is_deleted=0 AND %s" % where, args).fetchall()
    cmap = _cats_map(conn)
    tags = _tags_for(conn, "task", [r["id"] for r in rows])
    return [task_row(r, tags[r["id"]], cmap.get(r["category_id"]), today)
            for r in rows]


def _has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        [name]).fetchone() is not None


def _history_add(conn, task_id, action, detail, ts=None):
    conn.execute(
        "INSERT INTO biz_task_history(task_id, action, detail, created_at)"
        " VALUES (?,?,?,?)",
        (task_id, action,
         detail if isinstance(detail, str) else json.dumps(detail,
                                                           ensure_ascii=False),
         ts or now()))


def _source_titles(conn):
    """来源标题映射，用于提醒展示。"""
    skey = {}
    for key, table in (("task", "biz_task"), ("event", "biz_calendar_event")):
        for r in conn.execute("SELECT id, title FROM %s" % table):
            skey[(key, r["id"])] = r["title"]
    return skey


# ---------------------------------------------------------------- 设置

def get_settings(conn):
    out = {
        "start_page": "today",
        "default_lead": "30",
        "desktop_notify": "1",
        "keep_alive": "0",
    }
    for r in conn.execute("SELECT key, value FROM sys_setting"):
        out[r["key"]] = r["value"]
    return out


def save_settings(conn, data):
    allowed = ("start_page", "default_lead", "desktop_notify", "keep_alive")
    clean = {k: str(v) for k, v in (data or {}).items() if k in allowed}
    if not clean:
        raise UserError("没有可保存的设置项")
    with db.write_txn(conn):
        for k, v in clean.items():
            conn.execute(
                "INSERT INTO sys_setting(key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    return get_settings(conn)


# ---------------------------------------------------------------- 分类与标签

def cat_list(conn):
    rows = conn.execute(
        "SELECT * FROM sys_category ORDER BY sort, id").fetchall()
    counts = {r["category_id"]: r["n"] for r in conn.execute(
        "SELECT category_id, COUNT(*) n FROM biz_task"
        " WHERE is_deleted=0 AND status IN (0,1) GROUP BY category_id")}
    return [dict(r, open_tasks=counts.get(r["id"], 0)) for r in rows]


def cat_save(conn, data):
    name = (data.get("name") or "").strip()
    if not name:
        raise UserError("分类名称不能为空")
    color = data.get("color") or ""
    sort = _int_or(data.get("sort"), 0) or 0
    ts = now()
    with db.write_txn(conn):
        if data.get("id"):
            r = conn.execute("SELECT id FROM sys_category WHERE id=?",
                             [data["id"]]).fetchone()
            if not r:
                raise UserError("分类不存在")
            conn.execute(
                "UPDATE sys_category SET name=?, color=?, sort=?, updated_at=?"
                " WHERE id=?", (name, color, sort, ts, data["id"]))
            return data["id"]
        cur = conn.execute(
            "INSERT INTO sys_category(name, color, sort, created_at, updated_at)"
            " VALUES (?,?,?,?,?)", (name, color, sort, ts, ts))
        return cur.lastrowid


def cat_delete(conn, cat_id):
    """删除分类；关联待办/日程迁为未分类（PRD 4.7/5.4）。"""
    with db.write_txn(conn):
        r = conn.execute("SELECT name FROM sys_category WHERE id=?",
                         [cat_id]).fetchone()
        if not r:
            raise UserError("分类不存在")
        conn.execute("UPDATE biz_task SET category_id=NULL, updated_at=?"
                     " WHERE category_id=?", (now(), cat_id))
        conn.execute("UPDATE biz_calendar_event SET category_id=NULL,"
                     " updated_at=? WHERE category_id=?", (now(), cat_id))
        conn.execute("DELETE FROM sys_category WHERE id=?", [cat_id])
    return r["name"]


def tag_list(conn):
    counts = {r["tag_id"]: r["n"] for r in conn.execute(
        "SELECT it.tag_id, COUNT(*) n FROM sys_item_tag it"
        " JOIN biz_task t ON t.id=it.item_id AND t.is_deleted=0"
        " AND t.status IN (0,1) WHERE it.item_type='task'"
        " GROUP BY it.tag_id")}
    return [dict(r, open_tasks=counts.get(r["id"], 0)) for r in
            conn.execute("SELECT * FROM sys_tag ORDER BY id")]


def tag_add(conn, name):
    name = (name or "").strip()
    if not name:
        raise UserError("标签名称不能为空")
    n = conn.execute("SELECT COUNT(*) c FROM sys_tag").fetchone()["c"]
    with db.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO sys_tag(name, color_idx) VALUES (?,?)"
            " ON CONFLICT(name) DO NOTHING", (name, int(n) % 8))
        if cur.rowcount == 0:
            row = conn.execute("SELECT id FROM sys_tag WHERE name=?",
                               [name]).fetchone()
            return row["id"]
        return cur.lastrowid


def tag_delete(conn, tag_id):
    with db.write_txn(conn):
        conn.execute("DELETE FROM sys_tag WHERE id=?", [tag_id])


def _tag_assign_rows(conn, item_type, item_id, tag_ids):
    """在已有事务里设置标签集合（不自己开事务）。"""
    conn.execute("DELETE FROM sys_item_tag WHERE item_type=? AND item_id=?",
                 [item_type, item_id])
    for tid in tag_ids or []:
        ok = conn.execute("SELECT id FROM sys_tag WHERE id=?",
                          [tid]).fetchone()
        if ok:
            conn.execute(
                "INSERT OR IGNORE INTO sys_item_tag(item_type, item_id,"
                " tag_id) VALUES (?,?,?)", (item_type, item_id, tid))


def tag_assign(conn, item_type, item_id, tag_ids):
    """整体设置某事项的标签集合。"""
    with db.write_txn(conn):
        _tag_assign_rows(conn, item_type, item_id, tag_ids)


# ---------------------------------------------------------------- 待办

def _sort_tasks(tasks):
    # 先按优先级，再按时间倒序（新创建的排在前面，今天刚加的不会被压到列表底部）
    return sorted(tasks, key=lambda t: (-t["priority"], -(t["created_at"] or 0)))


def task_list(conn, flt="all", q=""):
    """按组组织的待办（原型 P04：today/overdue/tomorrow/later/nodate/done）。"""
    today = today_str()
    tasks = _load_tasks(conn, "1=1", [], today)
    if q:
        q = q.strip().lower()
        tasks = [t for t in tasks if q in t["title"].lower()
                 or q in (t["description"] or "").lower()]
    tomorrow = date_add(today, 1)
    open_ = [t for t in tasks if t["status"] in (0, 1)]
    done = [t for t in tasks if t["status"] == 2]

    def flt_keep(t):
        if flt == "todo":
            return t["status"] == 0
        if flt == "doing":
            return t["status"] == 1
        if flt == "done":
            return t["status"] == 2
        if flt == "done_manual":
            return t["status"] == 2 and t.get("completed_via") != "日报"
        if flt == "done_journal":
            return t["status"] == 2 and t.get("completed_via") == "日报"
        if flt == "overdue":
            return bool(t["overdue"])
        return True

    open_ = [t for t in open_ if flt_keep(t)]
    done = [t for t in done if flt_keep(t)]
    groups = {
        "today": _sort_tasks([t for t in open_ if t["planned_date"] == today]),
        "overdue": _sort_tasks([t for t in open_ if t["overdue"]]),
        "tomorrow": _sort_tasks([t for t in open_
                                 if t["planned_date"] == tomorrow]),
        "later": _sort_tasks([t for t in open_ if t["planned_date"]
                              and t["planned_date"] > tomorrow]),
        "nodate": _sort_tasks([t for t in open_ if not t["planned_date"]]),
        "done": sorted(done, key=lambda t: -(t["completed_at"] or 0))[:50],
    }
    if flt == "overdue":
        groups = {k: v for k, v in groups.items() if k == "overdue"}
    return {"today": today, "groups": groups,
            "count": sum(len(v) for v in groups.values())}


def task_get(conn, task_id):
    r = conn.execute("SELECT * FROM biz_task WHERE id=? AND is_deleted=0",
                     [task_id]).fetchone()
    if not r:
        raise UserError("待办不存在")
    t = task_row(r, _tags_for(conn, "task", [task_id])[task_id],
                 _cats_map(conn).get(r["category_id"]))
    t["history"] = [dict(h) for h in conn.execute(
        "SELECT * FROM biz_task_history WHERE task_id=?"
        " ORDER BY created_at DESC, id DESC LIMIT 30", [task_id])]
    t["reminders"] = [reminder_row(x) for x in conn.execute(
        "SELECT * FROM biz_reminder WHERE source_type='task' AND source_id=?"
        " AND status IN (0,1) ORDER BY trigger_at", [task_id])]
    t["events"] = [event_row(x, _cats_map(conn).get(x["category_id"]))
                   for x in conn.execute(
        "SELECT * FROM biz_calendar_event WHERE task_id=? AND is_deleted=0"
        " ORDER BY start_at", [task_id])]
    return t


def task_save(conn, data):
    title = (data.get("title") or "").strip()
    if not title:
        raise UserError("标题不能为空")
    status = _int_or(data.get("status"), 0)
    if status not in _STATUS:
        raise UserError("状态不合法")
    priority = _int_or(data.get("priority"), 1)
    if priority not in _PRIORITY:
        raise UserError("优先级不合法")
    planned = check_date(data.get("planned_date"))
    due = check_date(data.get("due_date"))
    due_time = check_hm(data.get("due_time"))
    if due_time and not due:
        raise UserError("填写截止时间需要先选择截止日期")
    est = _int_or(data.get("estimated_minutes"))
    cat_id = _int_or(data.get("category_id"))
    ts = now()
    old = None
    if data.get("id"):
        old = conn.execute(
            "SELECT * FROM biz_task WHERE id=? AND is_deleted=0",
            [data["id"]]).fetchone()
        if not old:
            raise UserError("待办不存在")
    with db.write_txn(conn):
        fields = dict(title=title, description=data.get("description") or "",
                      status=status, priority=priority, category_id=cat_id,
                      planned_date=planned, due_date=due, due_time=due_time,
                      estimated_minutes=est, updated_at=ts)
        if status == 2:
            # 直接保存为已完成：没有完成时间则补记（PRD 5.2）
            if old is None or old["completed_at"] is None:
                fields["completed_at"] = ts
                fields["completed_via"] = "手动"
        elif old is not None and old["status"] == 2 and status != 2:
            fields["completed_at"] = None
            fields["completed_via"] = None
        if data.get("id"):
            sets = ",".join("%s=?" % k for k in fields)
            conn.execute("UPDATE biz_task SET %s WHERE id=?" % sets,
                         list(fields.values()) + [data["id"]])
            tid = data["id"]
            changed = {k: [old[k], v] for k, v in fields.items()
                       if old[k] != v and k != "updated_at"}
            if changed:
                _history_add(conn, tid, "edit", changed, ts)
        else:
            fields["created_at"] = ts
            fields["created_via"] = "手动"
            cols = ",".join(fields)
            marks = ",".join("?" * len(fields))
            cur = conn.execute(
                "INSERT INTO biz_task(%s) VALUES (%s)" % (cols, marks),
                list(fields.values()))
            tid = cur.lastrowid
            _history_add(conn, tid, "created", {"title": title}, ts)
        if data.get("tag_ids") is not None:
            _tag_assign_rows(conn, "task", tid, data["tag_ids"])
    return tid


def task_meta_save(conn, data):
    """只改分类/标签：先读全量再复用 task_save，避免部分字段被清空。"""
    task_id = _int_or(data.get("id"))
    t = task_get(conn, task_id)
    payload = {
        "id": task_id,
        "title": t["title"],
        "description": t.get("description") or "",
        "status": t["status"],
        "priority": t.get("priority") or 1,
        "category_id": t.get("category_id"),
        "planned_date": t.get("planned_date"),
        "due_date": t.get("due_date"),
        "due_time": t.get("due_time"),
        "estimated_minutes": t.get("estimated_minutes"),
        "tag_ids": [x["id"] for x in (t.get("tags") or [])],
    }
    if "category_id" in data:
        payload["category_id"] = _int_or(data.get("category_id"))
    if "tag_ids" in data:
        payload["tag_ids"] = [_int_or(x) for x in (data.get("tag_ids") or [])]
    return task_save(conn, payload)


def task_meta_many(conn, data):
    """批量改分类/标签。tag_mode: add 追加 / remove 移除 / set 覆盖。"""
    ids = [_int_or(x) for x in (data.get("ids") or [])]
    if not ids:
        raise UserError("先选择待办")
    has_tags = data.get("tag_ids") is not None
    tag_ids = [_int_or(x) for x in (data.get("tag_ids") or [])]
    tag_mode = data.get("tag_mode") or "add"
    for tid in ids:
        d = {"id": tid}
        if "category_id" in data:
            d["category_id"] = data.get("category_id")
        if has_tags:
            cur = [x["id"] for x in task_get(conn, tid).get("tags") or []]
            if tag_mode == "add":
                d["tag_ids"] = cur + [x for x in tag_ids if x not in cur]
            elif tag_mode == "remove":
                d["tag_ids"] = [x for x in cur if x not in tag_ids]
            else:
                d["tag_ids"] = tag_ids
        task_meta_save(conn, d)
    return len(ids)


def task_set_status(conn, task_id, status):
    """状态流转（完成/重开/取消），记录完成时间与历史，历史不可覆盖。"""
    if status not in _STATUS:
        raise UserError("状态不合法")
    ts = now()
    with db.write_txn(conn):
        r = conn.execute("SELECT * FROM biz_task WHERE id=? AND is_deleted=0",
                         [task_id]).fetchone()
        if not r:
            raise UserError("待办不存在")
        completed_at = ts if status == 2 else None
        conn.execute(
            "UPDATE biz_task SET status=?, completed_at=?, completed_via=?,"
            " updated_at=? WHERE id=?",
            (status, completed_at, "手动" if status == 2 else None,
             ts, task_id))
        _history_add(conn, task_id, "status",
                     {"from": _STATUS_NAMES[r["status"]],
                      "to": _STATUS_NAMES[status], "at": epoch_to_hm(ts)}, ts)
        if status == 2:
            # 完成待办时，其未处理的提醒一并处理（PRD 场景 D）
            conn.execute(
                "UPDATE biz_reminder SET status=2, updated_at=?"
                " WHERE source_type='task' AND source_id=? AND status IN (0,1)",
                (ts, task_id))
    return {"id": task_id, "status": status, "completed_at": completed_at}


def task_delete(conn, task_id):
    """软删除；关联提醒标记失效（PRD 5.4）。"""
    ts = now()
    with db.write_txn(conn):
        r = conn.execute(
            "SELECT title FROM biz_task WHERE id=? AND is_deleted=0",
            [task_id]).fetchone()
        if not r:
            raise UserError("待办不存在")
        conn.execute("UPDATE biz_task SET is_deleted=1, updated_at=? WHERE id=?",
                     (ts, task_id))
        conn.execute("UPDATE biz_reminder SET status=3, updated_at=?"
                     " WHERE source_type='task' AND source_id=?"
                     " AND status IN (0,1)", (ts, task_id))
    return r["title"]


def task_delete_many(conn, task_ids):
    """批量软删除（待办页多选删除）。返回实际删除的标题。"""
    ids = []
    for i in task_ids or []:
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        if i > 0 and i not in ids:
            ids.append(i)
    if not ids:
        raise UserError("请先选择要删除的待办")
    ts = now()
    deleted = []
    with db.write_txn(conn):
        for i in ids:
            r = conn.execute(
                "SELECT title FROM biz_task WHERE id=? AND is_deleted=0",
                [i]).fetchone()
            if not r:
                continue
            conn.execute(
                "UPDATE biz_task SET is_deleted=1, updated_at=? WHERE id=?",
                (ts, i))
            conn.execute(
                "UPDATE biz_reminder SET status=3, updated_at=?"
                " WHERE source_type='task' AND source_id=?"
                " AND status IN (0,1)", (ts, i))
            deleted.append(r["title"])
    return {"count": len(deleted), "titles": deleted}


# ---------------------------------------------------------------- 收集箱

def inbox_list(conn):
    rows = conn.execute(
        "SELECT * FROM biz_inbox WHERE is_deleted=0 ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def inbox_save(conn, data):
    title = (data.get("title") or "").strip()
    if not title:
        raise UserError("标题不能为空")
    note = data.get("note") or ""
    ts = now()
    with db.write_txn(conn):
        if data.get("id"):
            r = conn.execute("SELECT id FROM biz_inbox WHERE id=?"
                             " AND is_deleted=0", [data["id"]]).fetchone()
            if not r:
                raise UserError("收集项不存在")
            conn.execute("UPDATE biz_inbox SET title=?, note=?, updated_at=?"
                         " WHERE id=?", (title, note, ts, data["id"]))
            return data["id"]
        cur = conn.execute(
            "INSERT INTO biz_inbox(title, note, created_at, updated_at)"
            " VALUES (?,?,?,?)", (title, note, ts, ts))
        return cur.lastrowid


def inbox_delete(conn, item_id):
    with db.write_txn(conn):
        conn.execute("UPDATE biz_inbox SET is_deleted=1, updated_at=?"
                     " WHERE id=?", (now(), item_id))


def inbox_convert(conn, data):
    """收集项 → 待办 / 日程，保留原始创建时间（PRD 4.2）。"""
    kind = data.get("kind")
    item_id = _int_or(data.get("id"))
    r = conn.execute("SELECT * FROM biz_inbox WHERE id=? AND is_deleted=0",
                     [item_id]).fetchone() if item_id else None
    if not r:
        raise UserError("收集项不存在")
    ts = now()
    with db.write_txn(conn):
        if kind == "task":
            cur = conn.execute(
                "INSERT INTO biz_task(title, description, status, priority,"
                " planned_date, created_via, created_at, updated_at)"
                " VALUES (?,?,0,1,?,'收集箱',?,?)",
                (r["title"], r["note"],
                 check_date(data.get("planned_date")), r["created_at"], ts))
            _history_add(conn, cur.lastrowid, "created",
                         {"title": r["title"], "via": "收集箱"}, ts)
        elif kind == "event":
            start = _int_or(data.get("start_at"))
            end = _int_or(data.get("end_at"))
            if not start or not end or end <= start:
                raise UserError("请提供有效的开始和结束时间")
            conn.execute(
                "INSERT INTO biz_calendar_event(title, description, start_at,"
                " end_at, is_all_day, created_at, updated_at)"
                " VALUES (?,?,?,?,0,?,?)",
                (r["title"], r["note"], start, end, r["created_at"], ts))
        else:
            raise UserError("转换类型不合法")
        conn.execute("UPDATE biz_inbox SET is_deleted=1, updated_at=?"
                     " WHERE id=?", (ts, item_id))
    return {"converted": kind, "id": item_id}


# ---------------------------------------------------------------- 日程

def events_range(conn, from_date, to_date):
    """按本地日期范围取日程（跨天事件与区间有交集即返回）。"""
    a = date_to_epoch(from_date, "00:00")
    b = date_to_epoch(to_date, "23:59")
    rows = conn.execute(
        "SELECT * FROM biz_calendar_event WHERE is_deleted=0"
        " AND end_at >= ? AND start_at <= ? ORDER BY start_at", (a, b)).fetchall()
    cmap = _cats_map(conn)
    return [event_row(r, cmap.get(r["category_id"])) for r in rows]


def event_save(conn, data):
    title = (data.get("title") or "").strip()
    if not title:
        raise UserError("标题不能为空")
    ts = now()
    all_day = 1 if data.get("is_all_day") else 0
    if all_day:
        d1 = check_date(data.get("start_date"))
        if not d1:
            raise UserError("请选择开始日期")
        d2 = check_date(data.get("end_date")) or d1
        if d2 < d1:
            raise UserError("结束日期不能早于开始日期")
        start_at = date_to_epoch(d1, "00:00")
        end_at = date_to_epoch(d2, "23:59")
    else:
        start_at = _int_or(data.get("start_at"))
        end_at = _int_or(data.get("end_at"))
        if not start_at or not end_at:
            raise UserError("请选择开始和结束时间")
        if end_at <= start_at:
            raise UserError("结束时间不能早于开始时间")
    cat_id = _int_or(data.get("category_id"))
    task_id = _int_or(data.get("task_id"))
    if task_id:
        ok = conn.execute("SELECT 1 FROM biz_task WHERE id=? AND is_deleted=0",
                          [task_id]).fetchone()
        if not ok:
            raise UserError("关联的待办不存在")
    with db.write_txn(conn):
        fields = dict(title=title, description=data.get("description") or "",
                      start_at=start_at, end_at=end_at, is_all_day=all_day,
                      category_id=cat_id, task_id=task_id, updated_at=ts)
        if data.get("id"):
            old = conn.execute(
                "SELECT * FROM biz_calendar_event WHERE id=? AND is_deleted=0",
                [data["id"]]).fetchone()
            if not old:
                raise UserError("日程不存在")
            sets = ",".join("%s=?" % k for k in fields)
            conn.execute("UPDATE biz_calendar_event SET %s WHERE id=?" % sets,
                         list(fields.values()) + [data["id"]])
            eid = data["id"]
        else:
            fields["created_at"] = ts
            cols = ",".join(fields)
            marks = ",".join("?" * len(fields))
            cur = conn.execute(
                "INSERT INTO biz_calendar_event(%s) VALUES (%s)" % (cols, marks),
                list(fields.values()))
            eid = cur.lastrowid
        # 日程提醒：先失效旧提醒，再按 lead 分钟重建（0=不提醒）
        lead = _int_or(data.get("remind_lead"), 0) or 0
        conn.execute(
            "UPDATE biz_reminder SET status=3, updated_at=? WHERE"
            " source_type='event' AND source_id=? AND status IN (0,1)", (ts, eid))
        if lead:
            conn.execute(
                "INSERT INTO biz_reminder(title, trigger_at, source_type,"
                " source_id, status, created_at, updated_at)"
                " VALUES (?,?,?,?,0,?,?)",
                (title, max(start_at - lead * 60, min(start_at, ts)),
                 "event", eid, ts, ts))
    return eid


def event_delete(conn, event_id):
    ts = now()
    with db.write_txn(conn):
        r = conn.execute(
            "SELECT title FROM biz_calendar_event WHERE id=? AND is_deleted=0",
            [event_id]).fetchone()
        if not r:
            raise UserError("日程不存在")
        conn.execute("UPDATE biz_calendar_event SET is_deleted=1, updated_at=?"
                     " WHERE id=?", (ts, event_id))
        conn.execute("UPDATE biz_reminder SET status=3, updated_at=?"
                     " WHERE source_type='event' AND source_id=?"
                     " AND status IN (0,1)", (ts, event_id))
    return r["title"]


# ---------------------------------------------------------------- 提醒

def reminders_all(conn):
    rows = conn.execute(
        "SELECT * FROM biz_reminder ORDER BY trigger_at").fetchall()
    now_ts = now()
    upcoming, fired, snoozed, done = [], [], [], []
    for r in rows:
        item = dict(r)
        if r["status"] == 0:
            if r["snoozed_until"] and r["snoozed_until"] > now_ts:
                item["snoozed"] = 1
                snoozed.append(item)
            else:
                upcoming.append(item)
        elif r["status"] == 1:
            fired.append(item)
        else:
            done.append(item)
    out = {"upcoming": upcoming, "fired": fired, "snoozed": snoozed,
           "done": list(reversed(done))[:80]}
    skey = _source_titles(conn)
    for group in out.values():
        for r in group:
            r["source_title"] = skey.get((r["source_type"], r["source_id"]), "")
    return out


def reminder_save(conn, data):
    title = (data.get("title") or "").strip()
    if not title:
        raise UserError("提醒内容不能为空")
    trigger = _int_or(data.get("trigger_at"))
    if not trigger:
        raise UserError("请设置触发时间")
    source_type = data.get("source_type") or None
    source_id = _int_or(data.get("source_id"))
    if source_type == "task" and source_id:
        ok = conn.execute("SELECT 1 FROM biz_task WHERE id=? AND is_deleted=0",
                          [source_id]).fetchone()
        if not ok:
            raise UserError("来源待办不存在")
    ts = now()
    with db.write_txn(conn):
        if data.get("id"):
            r = conn.execute("SELECT id FROM biz_reminder WHERE id=?",
                             [data["id"]]).fetchone()
            if not r:
                raise UserError("提醒不存在")
            conn.execute(
                "UPDATE biz_reminder SET title=?, trigger_at=?, status=0,"
                " snoozed_until=NULL, updated_at=? WHERE id=?",
                (title, trigger, ts, data["id"]))
            return data["id"]
        cur = conn.execute(
            "INSERT INTO biz_reminder(title, trigger_at, source_type,"
            " source_id, status, created_at, updated_at)"
            " VALUES (?,?,?,?,0,?,?)",
            (title, trigger, source_type, source_id, ts, ts))
        return cur.lastrowid


def reminder_snooze(conn, reminder_id, minutes=10):
    ts = now()
    until = ts + int(minutes or 10) * 60
    with db.write_txn(conn):
        r = conn.execute("SELECT id FROM biz_reminder WHERE id=?",
                         [reminder_id]).fetchone()
        if not r:
            raise UserError("提醒不存在")
        conn.execute(
            "UPDATE biz_reminder SET status=0, snoozed_until=?, updated_at=?"
            " WHERE id=?", (until, ts, reminder_id))
    return until


def reminder_settle(conn, reminder_id, status):
    """status: 2已处理 / 3已忽略。"""
    if status not in (2, 3):
        raise UserError("状态不合法")
    with db.write_txn(conn):
        conn.execute("UPDATE biz_reminder SET status=?, updated_at=? WHERE id=?",
                     (status, now(), reminder_id))


def fire_due(conn, now_ts=None):
    """把到点（含稍后提醒到期）的提醒标记为「已触发」，返回本次触发列表。

    稍后提醒在 snoozed_until 之前不得因原 trigger_at 再次触发。"""
    now_ts = now_ts or now()
    with db.write_txn(conn):
        rows = conn.execute(
            "SELECT * FROM biz_reminder WHERE status=0 AND ("
            " (snoozed_until IS NULL AND trigger_at <= ?) OR"
            " (snoozed_until IS NOT NULL AND snoozed_until <= ?))",
            (now_ts, now_ts)).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE biz_reminder SET status=1, last_triggered_at=?,"
                " snoozed_until=NULL, updated_at=? WHERE id=?",
                (now_ts, now_ts, r["id"]))
    if not rows:
        return []
    skey = _source_titles(conn)
    return [reminder_row(r, skey.get((r["source_type"], r["source_id"]), ""))
            for r in rows]


def notify_poll(conn, cursor=0):
    """启动补查 + 前端轮询：返回 cursor 之后新触发的提醒。"""
    fired = conn.execute(
        "SELECT * FROM biz_reminder WHERE status=1 AND last_triggered_at > ?"
        " ORDER BY last_triggered_at", [_int_or(cursor, 0) or 0]).fetchall()
    skey = _source_titles(conn)
    items = [reminder_row(r, skey.get((r["source_type"], r["source_id"]), ""))
             for r in fired]
    pending = conn.execute(
        "SELECT COUNT(*) c FROM biz_reminder WHERE status=1").fetchone()["c"]
    return {"now": now(), "fired": items, "pending": pending}


# ---------------------------------------------------------------- 每日记录

def journal_get(conn, date):
    date = check_date(date) or today_str()
    r = conn.execute("SELECT * FROM biz_journal WHERE journal_date=?",
                     [date]).fetchone()
    j = {"journal_date": date, "content": r["content"] if r else "",
         "plan": r["plan"] if r else "",
         "plan_state": (r["plan_state"] if r else "{}") or "{}"}
    # 自动汇总：当天完成的待办 + 当天日程（与手动内容分开，实时计算，PRD 4.6）
    a, b = date_to_epoch(date, "00:00"), date_to_epoch(date, "23:59")
    cmap = _cats_map(conn)
    j["auto_done"] = [
        {"id": r["id"], "title": r["title"], "cat": cmap.get(r["category_id"]),
         "completed_at": r["completed_at"]}
        for r in conn.execute(
            "SELECT * FROM biz_task WHERE is_deleted=0 AND status=2"
            " AND completed_at BETWEEN ? AND ? ORDER BY completed_at", (a, b))]
    j["auto_events"] = [
        event_row(r, cmap.get(r["category_id"])) for r in conn.execute(
            "SELECT * FROM biz_calendar_event WHERE is_deleted=0"
            " AND start_at <= ? AND end_at >= ? ORDER BY start_at", (b, a))]
    return j


def journal_written_dates(conn, month):
    """某月里写过「今日工作」的日期，供每日总结日历打点。"""
    m = (month or "").strip()
    if not re.match(r"^\d{4}-\d{2}$", m):
        m = today_str()[:7]
    rows = conn.execute(
        "SELECT journal_date FROM biz_journal"
        " WHERE journal_date LIKE ? AND TRIM(content) <> ''"
        " ORDER BY journal_date", (m + "-%",)).fetchall()
    return [r["journal_date"] for r in rows]


def journal_save(conn, date, content=None, plan=None):
    date = check_date(date) or today_str()
    ts = now()
    with db.write_txn(conn):
        r = conn.execute("SELECT * FROM biz_journal WHERE journal_date=?",
                         [date]).fetchone()
        if not r:
            conn.execute(
                "INSERT INTO biz_journal(journal_date, content, plan,"
                " created_at, updated_at) VALUES (?,?,?,?,?)",
                (date, content if content is not None else "",
                 plan if plan is not None else "", ts, ts))
        else:
            new_content = content if content is not None else r["content"]
            new_plan = plan if plan is not None else r["plan"]
            conn.execute(
                "UPDATE biz_journal SET content=?, plan=?, updated_at=?"
                " WHERE journal_date=?", (new_content, new_plan, ts, date))
    return journal_get(conn, date)


def _journal_title_key(s):
    """日报标题比对键：去掉列表符号并忽略所有空白差异。
    用户改标点/加空格后，同一行不会被误判成新事项。"""
    return re.sub(r"\s+", "", strip_line_marker(s or ""))


def _find_task_by_journal_title(conn, title, statuses):
    """按日报行标题查任务，兼容历史数据里带列表符号（- / * / 1.）的标题。"""
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        "SELECT id, title FROM biz_task WHERE is_deleted=0"
        " AND status IN (%s) ORDER BY id" % marks, list(statuses))
    target = _journal_title_key(title)
    for r in rows:
        if _journal_title_key(r["title"]) == target:
            return r
    return None


def _deleted_journal_match(conn, title, date):
    """当天由日报生成、已被用户删除的同名记录：删除即用户决定，不再重建。

    只认「这一天日报生成的记录」；别的日期删掉的同名任务（比如昨天完成过、
    后来又被删）不影响今天日报记一条。日报同步删掉的记录
    （history.action='sync'）不算用户删除，日报里重新写回会重新生成。
    """
    target = _journal_title_key(title)
    rows = conn.execute(
        "SELECT t.title FROM biz_task t WHERE t.is_deleted=1"
        " AND t.created_via='日报' AND t.planned_date=?"
        " AND NOT EXISTS (SELECT 1 FROM biz_task_history h"
        "   WHERE h.task_id=t.id AND h.action='sync')",
        (date,))
    return any(_journal_title_key(r["title"]) == target for r in rows)


def _find_task_by_journal_title_on_date(conn, title, a, b):
    """同一天里已完成、标题相同的任务（按天去重：别的日期的同名记录不算）。"""
    target = _journal_title_key(title)
    rows = conn.execute(
        "SELECT id, title FROM biz_task WHERE is_deleted=0 AND status=2"
        " AND completed_at BETWEEN ? AND ? ORDER BY id", (a, b))
    for r in rows:
        if _journal_title_key(r["title"]) == target:
            return r
    return None


def _journal_similarity(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def journal_reconcile(conn, date, content):
    """日报逐行与待办核对（PRD 4.6 / 5.2）。

    - 今天：同名未完成待办自动完成；没有对应待办的行生成已完成记录；
    - 往日：完成时间归到那天中午，只计入那一天；
    - 未来：只存文字，不生成完成记录；
    - 按天去重：只和「同一天」的完成记录比标题，别的日期写过同一件事
      不影响这一天；
    - 双向同步：日报里删掉/改写的行，同步删除/改名当天由日报生成的记录；
      手动勾选完成的任务不在此列。
    """
    date = check_date(date) or today_str()
    lines = []
    for raw in (content or "").splitlines():
        s = strip_line_marker(raw)
        if s:
            lines.append(s)
    out = {"completed": [], "created": [], "skipped": [],
           "renamed": [], "removed": []}
    if date > today_str():          # 未来日期只有文字，没有完成事实
        return out

    a, b = date_to_epoch(date, "00:00"), date_to_epoch(date, "23:59")
    ts = now()
    # 补记往日日报：完成时间归到那一天，不能算进「今天」的完成统计
    done_ts = ts if date == today_str() else date_to_epoch(date, "12:00")
    with db.write_txn(conn):
        # 这一天由日报生成的完成记录 —— 同步删除/改名的基准
        existing = conn.execute(
            "SELECT id, title FROM biz_task"
            " WHERE is_deleted=0 AND status=2 AND created_via='日报'"
            " AND planned_date=? AND completed_at BETWEEN ? AND ?"
            " ORDER BY id", (date, a, b)).fetchall()
        by_key = {}
        for r in existing:
            by_key.setdefault(_journal_title_key(r["title"]), []).append(r)
        kept_ids, pending, pending_keys = set(), [], set()

        for title in lines:
            key = _journal_title_key(title)
            bucket = by_key.get(key)
            if bucket:               # 这一天已经记过这一行
                kept_ids.add(bucket.pop(0)["id"])
                out["skipped"].append(title)
                continue
            # 兼容旧数据：标题可能残留「- 」「* 」「1. 」等列表符号，
            # 比对前统一去掉，避免同一行被反复建成新记录。
            row = _find_task_by_journal_title(conn, title, (0, 1))
            if row:
                conn.execute(
                    "UPDATE biz_task SET status=2, completed_at=?,"
                    " completed_via='日报', updated_at=? WHERE id=?",
                    (done_ts, ts, row["id"]))
                _history_add(conn, row["id"], "status",
                             {"to": "已完成", "via": "每日总结"}, done_ts)
                conn.execute(
                    "UPDATE biz_reminder SET status=2, updated_at=?"
                    " WHERE source_type='task' AND source_id=?"
                    " AND status IN (0,1)", (done_ts, row["id"]))
                out["completed"].append(title)
                continue
            # 同一天已有同名完成记录（手动勾选或日报记入）：不重复建
            if _find_task_by_journal_title_on_date(conn, title, a, b):
                out["skipped"].append(title)
                continue
            # 用户删掉过这条记录：尊重删除，不再从日报文本重新生成
            if _deleted_journal_match(conn, title, date):
                out["skipped"].append(title)
                continue
            if key in pending_keys:  # 日报里同一行写了两遍
                out["skipped"].append(title)
                continue
            pending.append(title)
            pending_keys.add(key)

        # 日报里已经删掉/改写的行：同步删除或改名对应的日报记录
        unmatched = [r for r in existing if r["id"] not in kept_ids]
        pairs = []
        for pi, title in enumerate(pending):
            for ri, r in enumerate(unmatched):
                ratio = _journal_similarity(title, r["title"])
                if ratio >= 0.7:     # 改动几个字算改名，保留原完成时间
                    pairs.append((ratio, pi, ri))
        pairs.sort(reverse=True)
        used_p, used_r = set(), set()
        for _, pi, ri in pairs:
            if pi in used_p or ri in used_r:
                continue
            used_p.add(pi)
            used_r.add(ri)
            old, new = unmatched[ri]["title"], pending[pi]
            conn.execute("UPDATE biz_task SET title=?, updated_at=? WHERE id=?",
                         (new, ts, unmatched[ri]["id"]))
            _history_add(conn, unmatched[ri]["id"], "edit",
                         {"title": [old, new], "via": "日报同步"}, ts)
            out["renamed"].append(new)
        for ri, r in enumerate(unmatched):
            if ri in used_r:
                continue
            conn.execute(
                "UPDATE biz_task SET is_deleted=1, updated_at=? WHERE id=?",
                (ts, r["id"]))
            conn.execute(
                "UPDATE biz_reminder SET status=3, updated_at=?"
                " WHERE source_type='task' AND source_id=?"
                " AND status IN (0,1)", (ts, r["id"]))
            _history_add(conn, r["id"], "sync",
                         {"title": r["title"], "reason": "日报中已删除该行"}, ts)
            out["removed"].append(r["title"])

        for pi, title in enumerate(pending):
            if pi in used_p:
                continue
            cur = conn.execute(
                "INSERT INTO biz_task(title, status, planned_date,"
                " completed_at, completed_via, created_via,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,'日报',?,?)",
                (title, 2, date, done_ts, "日报", done_ts, ts))
            _history_add(conn, cur.lastrowid, "created",
                         {"title": title, "via": "每日总结"}, done_ts)
            out["created"].append(title)
    return out


def plan_convert(conn, journal_date, index=None, action="task"):
    """「明日计划」行 → 待办（planned = 日报日期 + 1 天）或忽略。"""
    journal_date = check_date(journal_date) or today_str()
    r = conn.execute("SELECT * FROM biz_journal WHERE journal_date=?",
                     [journal_date]).fetchone()
    if not r or not (r["plan"] or "").strip():
        raise UserError("没有可转换的明日计划")
    lines = [l for l in (strip_line_marker(x) for x in r["plan"].splitlines())
             if l]
    try:
        state = json.loads(r["plan_state"] or "{}")
    except ValueError:
        state = {}
    ts = now()
    planned = date_add(journal_date, 1)
    targets = ([int(index)] if index is not None
               else [i for i in range(len(lines)) if str(i) not in state])
    made = []
    with db.write_txn(conn):
        for i in targets:
            if not 0 <= i < len(lines) or str(i) in state:
                continue
            title = lines[i].strip()
            if action == "task" and title:
                cur = conn.execute(
                    "INSERT INTO biz_task(title, status, priority, planned_date,"
                    " created_via, created_at, updated_at)"
                    " VALUES (?,?,1,?,'明日计划',?,?)",
                    (title, 0, planned, ts, ts))
                _history_add(conn, cur.lastrowid, "created",
                             {"title": title, "via": "明日计划"}, ts)
                made.append(title)
            state[str(i)] = "done" if action == "task" else "dismissed"
        conn.execute("UPDATE biz_journal SET plan_state=?, updated_at=?"
                     " WHERE journal_date=?",
                     (json.dumps(state, ensure_ascii=False), ts, journal_date))
    return {"made": made, "state": state}


def plan_restore(conn, journal_date, index):
    """撤销「忽略」：把这条计划恢复成待处理状态。"""
    journal_date = check_date(journal_date) or today_str()
    try:
        idx = int(index)
    except (TypeError, ValueError):
        raise UserError("请选择要撤销的计划")
    r = conn.execute("SELECT * FROM biz_journal WHERE journal_date=?",
                     [journal_date]).fetchone()
    if not r:
        raise UserError("找不到这一天的日报")
    try:
        state = json.loads(r["plan_state"] or "{}")
    except ValueError:
        state = {}
    key = str(idx)
    if state.get(key) != "dismissed":
        raise UserError("只有已忽略的计划可以撤销")
    with db.write_txn(conn):
        state.pop(key, None)
        conn.execute(
            "UPDATE biz_journal SET plan_state=?, updated_at=?"
            " WHERE journal_date=?",
            (json.dumps(state, ensure_ascii=False), now(), journal_date))
    return {"state": state}


# ---------------------------------------------------------------- 重要日子

def _next_occurrence(d, today=None):
    """返回 (下次发生日期 'YYYY-MM-DD'，还有几天)；过去且不重复返回 (None, -1)。"""
    today = today or today_str()
    if d.get("recur"):
        md = d["date"]  # 'MM-DD'
        cand = "%s-%s" % (today[:4], md)
        if cand < today:
            cand = "%d-%s" % (int(today[:4]) + 1, md)
        target = _date(cand)
    else:
        target = _date(d["date"])
        if target < _date(today):
            return None, -1
    delta = (target - _date(today)).days
    return target.strftime("%Y-%m-%d"), delta


def days_all(conn, today=None):
    today = today or today_str()
    upcoming, past = [], []
    for r in conn.execute("SELECT * FROM biz_special_day ORDER BY id"):
        item = dict(r)
        nxt, delta = _next_occurrence(item, today)
        item["next_date"] = nxt
        item["days_left"] = delta
        if delta is None or delta < 0:
            past.append(item)
        else:
            upcoming.append(item)
    upcoming.sort(key=lambda x: (x["days_left"], x["name"]))
    past.sort(key=lambda x: x["date"], reverse=True)
    return {"upcoming": upcoming, "past": past, "today": today}


def day_save(conn, data):
    name = (data.get("name") or "").strip()
    if not name:
        raise UserError("名称不能为空")
    dtype = data.get("type") or "自定义"
    recur = 1 if data.get("recur") else 0
    d = (data.get("date") or "").strip()
    if recur:
        try:
            _dt.datetime.strptime(d, "%m-%d")
        except (TypeError, ValueError):
            raise UserError("每年重复的日期格式应为 MM-DD")
    else:
        d = check_date(d)
        if not d:
            raise UserError("请选择日期")
    ts = now()
    with db.write_txn(conn):
        if data.get("id"):
            r = conn.execute("SELECT id FROM biz_special_day WHERE id=?",
                             [data["id"]]).fetchone()
            if not r:
                raise UserError("条目不存在")
            conn.execute(
                "UPDATE biz_special_day SET name=?, type=?, date=?, recur=?,"
                " updated_at=? WHERE id=?",
                (name, dtype, d, recur, ts, data["id"]))
            return data["id"]
        cur = conn.execute(
            "INSERT INTO biz_special_day(name, type, date, recur, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?)",
            (name, dtype, d, recur, ts, ts))
        return cur.lastrowid


def day_delete(conn, day_id):
    with db.write_txn(conn):
        conn.execute("DELETE FROM biz_special_day WHERE id=?", [day_id])


def days_banner(conn, today=None):
    """今天页面顶部的横幅数据（今天/明天到达的重要日子）。"""
    today = today or today_str()
    out = []
    for r in conn.execute("SELECT * FROM biz_special_day ORDER BY id"):
        nxt, delta = _next_occurrence(dict(r), today)
        if delta in (0, 1):
            out.append({"name": r["name"], "type": r["type"],
                        "days_left": delta, "next_date": nxt})
    out.sort(key=lambda x: x["days_left"])
    return out


# ---------------------------------------------------------------- 今天聚合

def today_data(conn, today=None):
    today = today or today_str()
    yesterday = date_add(today, -1)
    tasks = _load_tasks(conn, "1=1", [], today)
    open_ = [t for t in tasks if t["status"] in (0, 1)]
    done_today = [t for t in tasks if t["status"] == 2 and t["completed_at"]
                  and epoch_to_date(t["completed_at"]) == today]
    events = events_range(conn, today, today)
    tiles = {
        "total": len([t for t in open_ if t["planned_date"] == today]),
        "done": len(done_today),
        "done_manual": len([t for t in done_today
                            if t["completed_via"] != "日报"]),
        "done_journal": len([t for t in done_today
                             if t["completed_via"] == "日报"]),
        "overdue": len([t for t in open_ if t["overdue"]]),
        "events": len(events),
        "events_minutes": sum(
            max(0, (e["end_at"] - e["start_at"]) // 60)
            for e in events if not e["is_all_day"]),
    }
    trend = []
    for i in range(6, -1, -1):
        d = date_add(today, -i)
        a, b = date_to_epoch(d, "00:00"), date_to_epoch(d, "23:59")
        n = conn.execute(
            "SELECT COUNT(*) c FROM biz_task WHERE is_deleted=0 AND status=2"
            " AND completed_at BETWEEN ? AND ?", (a, b)).fetchone()["c"]
        trend.append({"date": d, "count": n, "is_today": d == today})
    rem = reminders_all(conn)
    important = rem["fired"][:3]
    upcoming_today = [r for r in rem["upcoming"]
                      if epoch_to_date(r["trigger_at"]) == today]
    yr = conn.execute("SELECT * FROM biz_journal WHERE journal_date=?",
                      [yesterday]).fetchone()
    plan = {"journal_date": yesterday, "lines": [], "state": {},
            "open_count": 0}
    if yr and (yr["plan"] or "").strip():
        plan["lines"] = [l for l in (strip_line_marker(x)
                                     for x in yr["plan"].splitlines()) if l]
        try:
            plan["state"] = json.loads(yr["plan_state"] or "{}")
        except ValueError:
            plan["state"] = {}
        plan["open_count"] = sum(
            1 for i in range(len(plan["lines"]))
            if str(i) not in plan["state"])
    return {
        "today": today,
        "weekday": _date(today).isoweekday(),
        "tiles": tiles,
        "trend": trend,
        "important": important,
        "upcoming_today": upcoming_today,
        "events": events,
        "tasks": _sort_tasks([t for t in open_
                              if t["planned_date"] == today]),
        "done": sorted(done_today, key=lambda t: -(t["completed_at"] or 0)),
        "plan": plan,
        "nodate_count": len([t for t in open_ if not t["planned_date"]]),
        "days_banner": days_banner(conn, today),
        "inbox_count": conn.execute(
            "SELECT COUNT(*) c FROM biz_inbox WHERE is_deleted=0"
        ).fetchone()["c"],
    }


# ---------------------------------------------------------------- 时间轴

def timeline_day(conn, date, kind="all"):
    date = check_date(date) or today_str()
    a, b = date_to_epoch(date, "00:00"), date_to_epoch(date, "23:59")
    cmap = _cats_map(conn)
    items = []
    if kind in ("all", "done"):
        for r in conn.execute(
                "SELECT * FROM biz_task WHERE is_deleted=0 AND status=2"
                " AND completed_at BETWEEN ? AND ?", (a, b)):
            items.append({"t": epoch_to_hm(r["completed_at"]), "kind": "done",
                          "title": r["title"],
                          "cat": cmap.get(r["category_id"]),
                          "ref": {"type": "task", "id": r["id"]}})
    if kind in ("all", "event"):
        for r in conn.execute(
                "SELECT * FROM biz_calendar_event WHERE is_deleted=0"
                " AND start_at <= ? AND end_at >= ?", (b, a)):
            items.append({"t": epoch_to_hm(r["start_at"]), "kind": "event",
                          "title": r["title"],
                          "meta": "全天" if r["is_all_day"] else "",
                          "cat": cmap.get(r["category_id"]),
                          "ref": {"type": "event", "id": r["id"]}})
    if kind in ("all", "remind"):
        for r in conn.execute(
                "SELECT * FROM biz_reminder WHERE last_triggered_at"
                " BETWEEN ? AND ?", (a, b)):
            items.append({"t": epoch_to_hm(r["last_triggered_at"]),
                          "kind": "remind", "title": r["title"],
                          "meta": "提醒触发",
                          "ref": {"type": "reminder", "id": r["id"]}})
    if kind in ("all", "note"):
        r = conn.execute(
            "SELECT content FROM biz_journal WHERE journal_date=?",
            [date]).fetchone()
        if r and (r["content"] or "").strip():
            first = (r["content"].strip().splitlines() or [""])[0][:40]
            items.append({"t": "", "kind": "note", "title": "每日记录",
                          "meta": first,
                          "ref": {"type": "journal", "id": date}})
    if kind in ("all", "day"):
        for r in conn.execute("SELECT * FROM biz_special_day"):
            nxt, _ = _next_occurrence(dict(r), date)
            if nxt == date:
                items.append({"t": "", "kind": "day", "title": r["name"],
                              "meta": r["type"], "ref": None})
    items.sort(key=lambda x: x["t"] or "99:99")
    total = {"done": sum(1 for i in items if i["kind"] == "done"),
             "event": sum(1 for i in items if i["kind"] == "event"),
             "remind": sum(1 for i in items if i["kind"] == "remind")}
    return {"date": date, "items": items, "total": total}


# ---------------------------------------------------------------- 驾驶舱

def dashboard(conn, rng="week"):
    today = today_str()
    open_tasks = [t for t in _load_tasks(conn, "1=1", [], today)
                  if t["status"] in (0, 1)]
    n = 7 if rng == "week" else 30
    trend = []
    for i in range(n - 1, -1, -1):
        d = date_add(today, -i)
        a, b = date_to_epoch(d, "00:00"), date_to_epoch(d, "23:59")
        cnt = conn.execute(
            "SELECT COUNT(*) c FROM biz_task WHERE is_deleted=0 AND status=2"
            " AND completed_at BETWEEN ? AND ?", (a, b)).fetchone()["c"]
        trend.append({"date": d, "count": cnt, "is_today": d == today})
    # 今日完成拆分：手动勾选 vs 每日总结记入（PRD 5.2 来源标记）
    done_manual = done_journal = 0
    for r in conn.execute(
            "SELECT completed_via, COUNT(*) c FROM biz_task"
            " WHERE is_deleted=0 AND status=2"
            " AND completed_at BETWEEN ? AND ? GROUP BY completed_via",
            (date_to_epoch(today, "00:00"), date_to_epoch(today, "23:59"))):
        if r["completed_via"] == "日报":
            done_journal += r["c"]
        else:
            done_manual += r["c"]
    cats = {}
    for t in open_tasks:
        key = t["cat"]["name"] if t["cat"] else "未分类"
        if key not in cats:
            cats[key] = {"name": key,
                         "color": t["cat"]["color"] if t["cat"] else "",
                         "count": 0}
        cats[key]["count"] += 1
    dist = sorted(cats.values(), key=lambda x: -x["count"])
    upcoming = events_range(conn, today, date_add(today, 7))
    journals = []
    for r in conn.execute(
            "SELECT * FROM biz_journal ORDER BY journal_date DESC LIMIT 8"):
        if (r["content"] or "").strip():
            journals.append({"date": r["journal_date"],
                             "snippet": (r["content"].strip().splitlines()
                                         or [""])[0][:50]})
    # 连续记录：从今天（或昨天）向前连续有日报内容的天数
    streak = 0
    d = today
    row = conn.execute("SELECT content FROM biz_journal WHERE journal_date=?",
                       [d]).fetchone()
    if not (row and (row["content"] or "").strip()):
        d = date_add(today, -1)
    while True:
        row = conn.execute(
            "SELECT content FROM biz_journal WHERE journal_date=?",
            [d]).fetchone()
        if row and (row["content"] or "").strip():
            streak += 1
            d = date_add(d, -1)
        else:
            break
    return {
        "tiles": {"open": len(open_tasks),
                  "doing": len([t for t in open_tasks if t["status"] == 1]),
                  "overdue": len([t for t in open_tasks if t["overdue"]]),
                  "today": len([t for t in open_tasks
                                if t["planned_date"] == today]),
                  "done_today": done_manual + done_journal,
                  "done_manual": done_manual,
                  "done_journal": done_journal,
                  "streak": streak},
        "trend": trend, "cats": dist, "upcoming": upcoming,
        "journals": journals, "range": rng,
    }


# ---------------------------------------------------------------- 搜索

def search(conn, q, stype="all", limit=40):
    q = (q or "").strip()
    if not q:
        return {"q": q, "groups": []}
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%") \
                  .replace("_", "\\_") + "%"
    esc = "ESCAPE '\\'"
    groups = []
    if stype in ("all", "task"):
        rows = conn.execute(
            "SELECT * FROM biz_task WHERE is_deleted=0 AND"
            " (title LIKE ? %s OR description LIKE ? %s)"
            " ORDER BY updated_at DESC LIMIT ?" % (esc, esc),
            (like, like, limit)).fetchall()
        cmap = _cats_map(conn)
        groups.append({"type": "task", "label": "待办",
                       "items": [{"id": r["id"], "title": r["title"],
                                  "date": r["planned_date"] or "",
                                  "status": r["status"],
                                  "cat": cmap.get(r["category_id"])}
                                 for r in rows]})
    if stype in ("all", "event"):
        rows = conn.execute(
            "SELECT * FROM biz_calendar_event WHERE is_deleted=0 AND"
            " (title LIKE ? %s OR description LIKE ? %s)"
            " ORDER BY start_at DESC LIMIT ?" % (esc, esc),
            (like, like, limit)).fetchall()
        groups.append({"type": "event", "label": "日程",
                       "items": [{"id": r["id"], "title": r["title"],
                                  "date": epoch_to_date(r["start_at"]),
                                  "start_hm": epoch_to_hm(r["start_at"]),
                                  "is_all_day": r["is_all_day"]}
                                 for r in rows]})
    if stype in ("all", "journal"):
        rows = conn.execute(
            "SELECT journal_date, content FROM biz_journal"
            " WHERE content LIKE ? %s ORDER BY journal_date DESC LIMIT ?" % esc,
            (like, limit)).fetchall()
        groups.append({"type": "journal", "label": "每日记录",
                       "items": [{"id": r["journal_date"],
                                  "title": r["journal_date"],
                                  "date": r["journal_date"],
                                  "snippet": (r["content"].strip()
                                              .splitlines() or [""])[0][:60]}
                                 for r in rows]})
    if stype in ("all", "inbox"):
        rows = conn.execute(
            "SELECT * FROM biz_inbox WHERE is_deleted=0 AND"
            " (title LIKE ? %s OR note LIKE ? %s)"
            " ORDER BY created_at DESC LIMIT ?" % (esc, esc),
            (like, like, limit)).fetchall()
        groups.append({"type": "inbox", "label": "收集箱",
                       "items": [{"id": r["id"], "title": r["title"],
                                  "date": epoch_to_date(r["created_at"])}
                                 for r in rows]})
    groups = [g for g in groups if g["items"]]
    return {"q": q, "groups": groups}


# ---------------------------------------------------------------- 引导数据

def bootstrap(conn):
    return {
        "version": _version(),
        "settings": get_settings(conn),
        "cats": cat_list(conn),
        "tags": tag_list(conn),
        "inbox_count": conn.execute(
            "SELECT COUNT(*) c FROM biz_inbox WHERE is_deleted=0"
        ).fetchone()["c"],
        "fired_count": conn.execute(
            "SELECT COUNT(*) c FROM biz_reminder WHERE status=1"
        ).fetchone()["c"],
        "db_path": db.db_path(),
        "data_dir": db.data_dir(),
        "today": today_str(),
    }


def _version():
    from . import __version__
    return __version__


# ---------------------------------------------------------------- 迷你 CLI

def main(argv=None):
    setup_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    db.init_db()
    conn = db.connect()
    try:
        cmd = argv[0] if argv else "today"
        if cmd == "today":
            d = today_data(conn)
            print("今天 %s · 待办 %d 已完成 %d 逾期 %d 日程 %d" % (
                d["today"], d["tiles"]["total"], d["tiles"]["done"],
                d["tiles"]["overdue"], d["tiles"]["events"]))
            for t in d["tasks"]:
                print("  [ ] %s（%s）" % (t["title"], t["priority_name"]))
            for t in d["done"]:
                print("  [x] %s（%s 完成）" % (
                    t["title"], epoch_to_hm(t["completed_at"])))
        elif cmd == "list":
            res = task_list(conn)
            for gname, items in res["groups"].items():
                if not items:
                    continue
                print("## %s（%d）" % (gname, len(items)))
                for t in items:
                    print("  #%s %s（%s·%s）" % (
                        t["id"], t["title"], t["status_name"],
                        t["priority_name"]))
        elif cmd == "backup":
            print("已备份到 %s" % db.backup_now())
        elif cmd == "stats":
            print("数据库：%s" % db.db_path())
            for table in ("biz_task", "biz_calendar_event", "biz_reminder",
                          "biz_journal", "biz_inbox", "biz_special_day"):
                n = conn.execute(
                    "SELECT COUNT(*) c FROM %s" % table).fetchone()["c"]
                print("  %-20s %d 行" % (table, n))
        else:
            print("用法：personal-workspace today | list | backup | stats")
            return 2
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
