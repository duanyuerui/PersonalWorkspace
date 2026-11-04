"""本地 HTTP 服务：静态 webui + /api 路由 + 应用窗口生命周期 + 提醒调度。

整体模式与 AI-SessionManager 一致：
- ThreadingHTTPServer 只绑 127.0.0.1；Host/Origin 校验；body 上限 1MB；
- 路由用 if/elif 直排，API 处理函数返回纯 dict；
- --app 模式用 Chromium --app 开无地址栏窗口，页面 pagehide 时发
  /api/bye，3 秒无新请求才退出（刷新不算关闭）；
- 本项目新增：提醒调度线程（每 15s 把到期提醒置为已触发）与
  「后台驻留」（设置 keep_alive=1 时关窗不退出，由 PowerShell 弹 Toast）。
"""
import argparse
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import core, db, notify

HERE = os.path.dirname(os.path.abspath(__file__))

LAST_HIT = [0.0]    # 最近一次请求时间（/api/bye 除外）
CLOSE_AT = [0.0]    # 页面离开时间（仅 app 模式）
APP_MODE = [False]
SERVER = [None]     # ThreadingHTTPServer 引用，供 /api/shutdown 使用
LOG = logging.getLogger("personal_workspace")

STATIC_TYPES = {".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml"}
MAX_BODY = 1_000_000


def _webui_root():
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen and os.path.isdir(os.path.join(frozen, "webui")):
        return os.path.join(frozen, "webui")
    return os.path.join(HERE, "webui")


UI_FILE = os.path.join(_webui_root(), "index.html")


LOG_KEEP_DAYS = 15


def _prune_logs(log_dir, keep_days=LOG_KEEP_DAYS):
    """按修改时间清理超过保留期的日志（含旧版按大小轮转的文件）。"""
    cutoff = time.time() - keep_days * 86400
    removed = 0
    try:
        names = os.listdir(log_dir)
    except OSError:
        return 0
    for name in names:
        if not name.startswith("app.log"):
            continue
        path = os.path.join(log_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def _log_handler():
    try:
        log_dir = os.path.join(db.data_dir(), "Logs")
        os.makedirs(log_dir, exist_ok=True)
        _prune_logs(log_dir)
        return logging.handlers.TimedRotatingFileHandler(
            os.path.join(log_dir, "app.log"), when="midnight",
            interval=1, backupCount=LOG_KEEP_DAYS - 1, encoding="utf-8",
            delay=True)
    except Exception:  # noqa: BLE001
        return None


def _setup_logging():
    handler = _log_handler()
    if handler is not None:
        logging.basicConfig(level=logging.INFO, handlers=[handler], format=(
            "%(asctime)s %(levelname)s %(message)s"))
    else:
        logging.basicConfig(level=logging.INFO)


def _clear_logs():
    """清空 Logs 目录下的 app.log 及轮转备份（先释放文件句柄）。"""
    for lg in (logging.getLogger(), LOG):
        for h in list(lg.handlers):
            if isinstance(h, logging.handlers.BaseRotatingHandler):
                lg.removeHandler(h)
                try:
                    h.close()
                except Exception:  # noqa: BLE001
                    pass
    log_dir = os.path.join(db.data_dir(), "Logs")
    removed = 0
    if os.path.isdir(log_dir):
        for name in os.listdir(log_dir):
            if not name.startswith("app.log"):
                continue
            try:
                os.remove(os.path.join(log_dir, name))
                removed += 1
            except OSError:
                pass
    handler = _log_handler()
    if handler is not None:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(handler)
    return {"removed": removed, "path": log_dir}


def _say(msg):
    """窗口版 exe 没有 stdout，打印要能安静跳过。"""
    try:
        print(msg)
    except Exception:  # noqa: BLE001
        pass


def _keep_alive(conn=None):
    """读当前「后台驻留」设置（低频，直接查库）。"""
    own = conn is None
    if own:
        conn = db.connect()
    try:
        row = conn.execute(
            "SELECT value FROM sys_setting WHERE key='keep_alive'").fetchone()
        return bool(row and str(row["value"]) in ("1", "true", "on"))
    except Exception:  # noqa: BLE001
        return False
    finally:
        if own:
            conn.close()


def _schedule_tick():
    """提醒调度：到点即置「已触发」；后台驻留且窗口不在时弹系统 Toast。"""
    conn = db.connect()
    try:
        fired = core.fire_due(conn)
    finally:
        conn.close()
    if not fired:
        return
    LOG.info("reminders fired: %s", [r["title"] for r in fired])
    window_open = time.time() - LAST_HIT[0] < 60
    if _keep_alive() and not window_open:
        for r in fired:
            src = "（%s）" % r["source_title"] if r["source_title"] else ""
            notify.fire("提醒：%s" % r["title"],
                        "%s已触发，打开工作台处理" % src)


def _scheduler():
    while True:
        try:
            _schedule_tick()
        except Exception as e:  # noqa: BLE001
            LOG.exception("scheduler error: %s", e)
        time.sleep(15)


# ================================================================ API 实现

def bootstrap_json():
    conn = db.connect()
    try:
        return core.bootstrap(conn)
    finally:
        conn.close()


def today_json():
    conn = db.connect()
    try:
        return core.today_data(conn)
    finally:
        conn.close()


def tasks_json(qs):
    conn = db.connect()
    try:
        return core.task_list(conn, qs.get("filter", "all"), qs.get("q", ""))
    finally:
        conn.close()


def task_json(qs):
    conn = db.connect()
    try:
        return core.task_get(conn, int(qs.get("id", "0") or 0))
    finally:
        conn.close()


def events_json(qs):
    conn = db.connect()
    try:
        return {"events": core.events_range(conn, qs.get("from") or
                                            core.today_str(),
                                            qs.get("to") or core.today_str())}
    finally:
        conn.close()


def reminders_json():
    conn = db.connect()
    try:
        return core.reminders_all(conn)
    finally:
        conn.close()


def journal_json(qs):
    conn = db.connect()
    try:
        data = core.journal_get(conn, qs.get("date") or core.today_str())
        # 本地排查用：能看出页面到底请求了哪天、有没有内容
        print("[journal] %s len=%d plan=%d" % (
            data["journal_date"], len(data["content"] or ""),
            len(data["plan"] or "")), flush=True)
        return data
    finally:
        conn.close()


def journal_written_json(qs):
    conn = db.connect()
    try:
        return {"dates": core.journal_written_dates(conn, qs.get("month"))}
    finally:
        conn.close()


def simple_json(fn):
    conn = db.connect()
    try:
        return fn(conn)
    finally:
        conn.close()


def notify_json(qs):
    conn = db.connect()
    try:
        return core.notify_poll(conn, qs.get("cursor", "0"))
    finally:
        conn.close()


def search_json(qs):
    conn = db.connect()
    try:
        return core.search(conn, qs.get("q", ""), qs.get("type", "all"))
    finally:
        conn.close()


def timeline_json(qs):
    conn = db.connect()
    try:
        return core.timeline_day(conn, qs.get("date") or core.today_str(),
                                 qs.get("kind", "all"))
    finally:
        conn.close()


def dashboard_json(qs):
    conn = db.connect()
    try:
        return core.dashboard(conn, qs.get("range", "week"))
    finally:
        conn.close()


def backups_json():
    return {"backups": db.list_backups(),
            "dir": db.backups_dir()}


def _with_conn_post(fn, body):
    conn = db.connect()
    try:
        out = fn(conn, body)
        return out if out is not None else {"ok": True}
    finally:
        conn.close()


def save_backup_json(body):
    path = db.backup_now(tag=(body.get("tag") or "").strip() or "")
    LOG.info("backup -> %s", path)
    return {"path": path, "backups": db.list_backups()}


def restore_json(body):
    name = os.path.basename(body.get("name") or "")
    if not name or not name.endswith(".db"):
        raise core.UserError("请提供备份文件名")
    path = os.path.join(db.backups_dir(), name)
    used, pre = db.restore_from(path)
    LOG.info("restore %s (pre-backup %s)", used, pre)
    return {"restored": used, "pre_backup": pre}


# ================================================================ Handler

class Handler(BaseHTTPRequestHandler):
    server_version = "PersonalWorkspace/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默；顺带记录活跃时间
        if not self.path.startswith("/api/bye"):
            LAST_HIT[0] = time.time()

    # ---- 输出助手 ----
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, "application/json; charset=utf-8", body)

    def _html(self, text, code=200):
        self._send(code, "text/html; charset=utf-8", text.encode("utf-8"))

    def _static(self, path, ctype):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self._json({"error": "not found"}, 404)
        self._send(200, ctype, data)

    # ---- 安全校验 ----
    def _local(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "::1")

    def _origin_ok(self):
        site = self.headers.get("Sec-Fetch-Site")
        if site in ("same-origin", "none"):
            return True
        origin = self.headers.get("Origin") or ""
        return origin.startswith(("http://127.0.0.1", "http://localhost",
                                  "http://[::1]"))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            raise core.UserError("请求体过大")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise core.UserError("请求体不是合法 JSON")

    # ---- 路由 ----
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
        try:
            if not self._local():
                return self._json({"error": "forbidden"}, 403)
            p = u.path
            if p in ("/", "/index.html"):
                try:
                    with open(UI_FILE, "r", encoding="utf-8") as f:
                        return self._html(f.read())
                except OSError:
                    return self._json({"error": "缺少界面文件"}, 500)
            if p.startswith("/webui/vendor/"):
                name = u.path[len("/webui/vendor/"):]
                if os.path.basename(name) != name:
                    return self._json({"error": "forbidden"}, 403)
                ext = os.path.splitext(name)[1].lower()
                if ext not in STATIC_TYPES:
                    return self._json({"error": "not found"}, 404)
                return self._static(os.path.join(_webui_root(), "vendor", name),
                                    STATIC_TYPES[ext])
            if not p.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            if p == "/api/bootstrap":
                return self._json(bootstrap_json())
            if p == "/api/today":
                return self._json(today_json())
            if p == "/api/tasks":
                return self._json(tasks_json(qs))
            if p == "/api/task":
                return self._json(task_json(qs))
            if p == "/api/events":
                return self._json(events_json(qs))
            if p == "/api/reminders":
                return self._json(reminders_json())
            if p == "/api/journal":
                return self._json(journal_json(qs))
            if p == "/api/journal/written":
                return self._json(journal_written_json(qs))
            if p == "/api/ui-version":
                # 前端轮询：界面文件一变就自动刷新，避免旧页面继续跑旧脚本
                try:
                    st = os.stat(UI_FILE)
                    return self._json({"v": "%d-%d" % (int(st.st_mtime),
                                                      st.st_size)})
                except OSError:
                    return self._json({"v": ""})
            if p == "/api/inbox":
                return self._json({"items": simple_json(core.inbox_list)})
            if p == "/api/cats":
                return self._json({"cats": simple_json(core.cat_list)})
            if p == "/api/tags":
                return self._json({"tags": simple_json(core.tag_list)})
            if p == "/api/days":
                return self._json(simple_json(core.days_all))
            if p == "/api/timeline":
                return self._json(timeline_json(qs))
            if p == "/api/dashboard":
                return self._json(dashboard_json(qs))
            if p == "/api/search":
                return self._json(search_json(qs))
            if p == "/api/notify":
                return self._json(notify_json(qs))
            if p == "/api/backups":
                return self._json(backups_json())
            return self._json({"error": "not found"}, 404)
        except (core.UserError, ValueError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            LOG.exception("GET %s failed", p)
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        try:
            if not self._local():
                return self._json({"error": "forbidden"}, 403)
            if not self._origin_ok():
                return self._json({"error": "forbidden"}, 403)
            p = u.path
            body = self._body()

            if p == "/api/bye":
                if APP_MODE[0] and u.query == "app=1":
                    CLOSE_AT[0] = time.time()
                return self._json({"ok": True})
            if p == "/api/shutdown":
                srv = SERVER[0]
                if srv is not None:
                    threading.Thread(target=srv.shutdown,
                                     daemon=True).start()
                return self._json({"ok": True})

            if p == "/api/task/save":
                tid = _with_conn_post(core.task_save, body)
                return self._json({"id": tid})
            if p == "/api/task/complete":
                return self._json(_with_conn_post(
                    lambda c, b: core.task_set_status(c, int(b.get("id", 0)), 2),
                    body))
            if p == "/api/task/reopen":
                return self._json(_with_conn_post(
                    lambda c, b: core.task_set_status(c, int(b.get("id", 0)), 0),
                    body))
            if p == "/api/task/status":
                return self._json(_with_conn_post(
                    lambda c, b: core.task_set_status(
                        c, int(b.get("id", 0)), int(b.get("status", 0))), body))
            if p == "/api/task/meta":
                return self._json({"id": _with_conn_post(
                    core.task_meta_save, body)})
            if p == "/api/task/meta_many":
                return self._json({"count": _with_conn_post(
                    core.task_meta_many, body)})
            if p == "/api/logs/clear":
                return self._json(_clear_logs())
            if p == "/api/task/delete":
                return self._json({"title": _with_conn_post(
                    lambda c, b: core.task_delete(c, int(b.get("id", 0))), body)})
            if p == "/api/task/delete_many":
                return self._json(_with_conn_post(
                    lambda c, b: core.task_delete_many(c, b.get("ids") or []),
                    body))
            if p == "/api/event/save":
                return self._json({"id": _with_conn_post(core.event_save, body)})
            if p == "/api/event/delete":
                return self._json({"title": _with_conn_post(
                    lambda c, b: core.event_delete(c, int(b.get("id", 0))), body)})
            if p == "/api/reminder/save":
                return self._json({"id": _with_conn_post(core.reminder_save,
                                                         body)})
            if p == "/api/reminder/snooze":
                return self._json({"until": _with_conn_post(
                    lambda c, b: core.reminder_snooze(
                        c, int(b.get("id", 0)), int(b.get("minutes", 10) or 10)),
                    body)})
            if p == "/api/reminder/settle":
                return self._json(_with_conn_post(
                    lambda c, b: core.reminder_settle(
                        c, int(b.get("id", 0)), int(b.get("status", 2))), body))
            if p == "/api/journal/save":
                return self._json(_with_conn_post(
                    lambda c, b: core.journal_save(
                        c, b.get("date"), b.get("content"), b.get("plan")),
                    body))
            if p == "/api/journal/reconcile":
                return self._json(_with_conn_post(
                    lambda c, b: core.journal_reconcile(
                        c, b.get("date"), b.get("content")), body))
            if p == "/api/journal/plan-convert":
                return self._json(_with_conn_post(
                    lambda c, b: core.plan_convert(
                        c, b.get("journal_date"),
                        None if b.get("index") is None else int(b["index"]),
                        b.get("action", "task")), body))
            if p == "/api/journal/plan-restore":
                return self._json(_with_conn_post(
                    lambda c, b: core.plan_restore(
                        c, b.get("journal_date"), b.get("index")), body))
            if p == "/api/inbox/save":
                return self._json({"id": _with_conn_post(core.inbox_save, body)})
            if p == "/api/inbox/delete":
                return self._json(_with_conn_post(
                    lambda c, b: core.inbox_delete(c, int(b.get("id", 0))), body))
            if p == "/api/inbox/convert":
                return self._json(_with_conn_post(core.inbox_convert, body))
            if p == "/api/cat/save":
                return self._json({"id": _with_conn_post(core.cat_save, body)})
            if p == "/api/cat/delete":
                return self._json({"name": _with_conn_post(
                    lambda c, b: core.cat_delete(c, int(b.get("id", 0))), body)})
            if p == "/api/tag/add":
                return self._json({"id": _with_conn_post(
                    lambda c, b: core.tag_add(c, b.get("name")), body)})
            if p == "/api/tag/delete":
                return self._json(_with_conn_post(
                    lambda c, b: core.tag_delete(c, int(b.get("id", 0))), body))
            if p == "/api/tag/assign":
                return self._json(_with_conn_post(
                    lambda c, b: core.tag_assign(
                        c, b.get("item_type", "task"), int(b.get("item_id", 0)),
                        b.get("tag_ids") or []), body))
            if p == "/api/day/save":
                return self._json({"id": _with_conn_post(core.day_save, body)})
            if p == "/api/day/delete":
                return self._json(_with_conn_post(
                    lambda c, b: core.day_delete(c, int(b.get("id", 0))), body))
            if p == "/api/settings/save":
                return self._json({"settings": _with_conn_post(core.save_settings,
                                                               body)})
            if p == "/api/backup":
                return self._json(save_backup_json(body))
            if p == "/api/restore":
                return self._json(restore_json(body))
            return self._json({"error": "not found"}, 404)
        except (core.UserError, ValueError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            LOG.exception("POST %s failed", p)
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)


# ================================================================ 窗口与服务

def _chromium():
    """找支持 --app 的 Chromium 内核浏览器（Edge / Chrome 都行）。"""
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("ProgramFiles"),
                     os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            cands.append(os.path.join(base, "Microsoft", "Edge", "Application",
                                      "msedge.exe"))
            cands.append(os.path.join(base, "Google", "Chrome", "Application",
                                      "chrome.exe"))
    elif sys.platform == "darwin":
        cands += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    for name in ("msedge", "chrome", "chromium", "google-chrome",
                 "microsoft-edge", "chromium-browser"):
        try:
            found = shutil.which(name)
        except Exception:  # noqa: BLE001
            found = None
        if found:
            cands.append(found)
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None


def open_app_window(url):
    """用浏览器的 --app 模式开一个没有地址栏/标签栏的窗口，像桌面客户端。"""
    exe = _chromium()
    if not exe:
        return None
    tag = url.rsplit(":", 1)[-1]
    profile = os.path.join(db.data_dir(), "app-profile-%s" % tag)
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError:
        profile = tempfile.mkdtemp(prefix="pws-app-")
    # 精简参数：去后台服务、省内存。单页本地应用不需要多渲染进程，
    # 也不该让 V8 堆无限增长。
    lean_flags = [
        "--disable-gpu",
        "--disable-extensions",
        "--disable-sync",
        "--disable-component-update",
        "--disable-background-networking",
        "--disable-breakpad",
        "--disable-client-side-phishing-detection",
        "--disable-default-apps",
        "--no-service-autorun",
        "--metrics-recording-only",
        "--process-per-site",
        "--renderer-process-limit=1",
        "--js-flags=--max-old-space-size=128",
        "--disable-features=msEdgeSearchIndexer,msEdgeSidebarV2,Translate,"
        "OptimizationHints,MediaRouter",
    ]
    args = [
        exe, "--app=" + url + "?app=1",
        "--user-data-dir=" + profile,
        # 注意：--window-size 会覆盖 --start-maximized（实测 Edge 里
        # 两者同时给时窗口仍是 1280x860），所以只保留最大化。
        "--start-maximized",
        "--no-first-run", "--no-default-browser-check",
        "--disable-background-mode",
    ] + lean_flags
    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return None


def _port_busy(port, timeout=0.15):
    """端口是否已有服务在监听。

    Windows 的 SO_REUSEADDR 允许两个进程同时绑定同一端口，直接 bind
    会“成功”，导致新旧实例共用端口、请求随机落到旧代码上。这里先探一下。
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout):
            return True
    except OSError:
        return False


def serve(root=None, port=8766, open_browser=True, app_window=False,
          idle_exit=1800):
    """启动服务；app_window=True 时用客户端窗口打开并跟随窗口退出。"""
    _setup_logging()
    db.init_db()
    if not os.path.isfile(UI_FILE):
        _say("缺少界面文件: %s" % UI_FILE)
        return 2
    httpd = None
    last_err = None
    for p in range(port, port + 20):
        if _port_busy(p):
            last_err = OSError("端口 %d 已被占用" % p)
            continue
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError as e:
            last_err = e
    if not httpd:
        _say("端口都被占用: %s" % last_err)
        return 1
    SERVER[0] = httpd
    url = "http://127.0.0.1:%d" % port
    _say("个人工作台: %s" % url)
    _say("数据库:     %s" % db.db_path())
    _say("按 Ctrl+C 退出（直接关闭这个窗口也可以）")
    LOG.info("serve at %s (app=%s)", url, app_window)

    threading.Thread(target=_scheduler, daemon=True).start()

    proc = None
    if app_window:
        APP_MODE[0] = True
        proc = open_app_window(url)
        if proc is None:
            open_browser = True
        else:
            open_browser = False

        # 页面关闭会发 /api/bye；3 秒无新请求才退出（后台驻留时不退出）
        def _watch_close():
            while True:
                time.sleep(1)
                if _keep_alive():
                    continue
                t = CLOSE_AT[0]
                if t and time.time() - t > 3 and LAST_HIT[0] <= t:
                    try:
                        httpd.shutdown()
                    except Exception:  # noqa: BLE001
                        pass
                    return

        threading.Thread(target=_watch_close, daemon=True).start()

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    if proc is not None:
        # 窗口一关，浏览器进程就退出 → 服务跟着停（后台驻留时不退出）
        def _watch_window():
            time.sleep(2.5)
            # Edge 已在运行时新窗口交给已有进程，Popen 会立刻退出：
            # 这种情况交给 /api/bye 判断，别据此关服务
            if proc.poll() is not None:
                return
            try:
                proc.wait()
            except Exception:  # noqa: BLE001
                pass
            while _keep_alive():
                time.sleep(5)
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_watch_window, daemon=True).start()
    elif app_window and idle_exit:
        # 退回默认浏览器时感知不到窗口关闭：长时间没人访问就自己退出
        LAST_HIT[0] = time.time()

        def _watch_idle():
            while True:
                time.sleep(60)
                if _keep_alive():
                    continue
                if time.time() - LAST_HIT[0] > idle_exit:
                    try:
                        httpd.shutdown()
                    except Exception:  # noqa: BLE001
                        pass
                    return

        threading.Thread(target=_watch_idle, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _say("\n已退出")
    finally:
        httpd.server_close()
        SERVER[0] = None
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="personal_workspace", description="个人工作台网页界面（本地）")
    ap.add_argument("--port", type=int, default=8766, help="端口，默认 8766")
    ap.add_argument("--no-browser", action="store_true",
                    help="不自动打开浏览器")
    ap.add_argument("--app", action="store_true",
                    help="用独立客户端窗口打开（Chromium 的 --app 模式）")
    a = ap.parse_args(argv)
    return serve(None, a.port, not a.no_browser, app_window=a.app)


if __name__ == "__main__":
    sys.exit(main())
