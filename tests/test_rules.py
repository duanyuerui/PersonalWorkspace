"""核心业务规则单元测试（纯标准库 unittest）。

覆盖 PRD §14 验收用例背后的规则：
- 待办日期规则（计划日期 ≠ 截止日期、今日归属、逾期展示状态）
- 完成历史不可覆盖
- 每日记录 reconcile 幂等
- 明日计划 → 待办（planned = 日报日期 + 1）
- 提醒触发 / 稍后提醒 / 补查
- 备份恢复一致性
- 日程校验（结束不早于开始）与提醒 lead

运行：
    python -m unittest discover -s tests -v
（测试用 PWS_DATA_DIR 指向临时目录，不碰真实数据。）
"""
import collections
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

_tmp = tempfile.mkdtemp(prefix="pws-test-")
os.environ["PWS_DATA_DIR"] = _tmp

from personal_workspace import core, db  # noqa: E402


class RulesTest(unittest.TestCase):
    def setUp(self):
        # 每个用例独立数据库，避免相互污染
        for f in ("workspace.db", "workspace.db-wal", "workspace.db-shm"):
            p = os.path.join(_tmp, f)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        db.init_db()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()

    # ---- 日期规则 ----
    def test_date_helpers(self):
        self.assertEqual(core.date_add("2026-11-02", 1), "2026-11-03")
        self.assertEqual(core.date_add("2026-11-30", 7), "2026-12-07")
        self.assertEqual(core.date_add("2026-11-02", -1), "2026-11-01")
        with self.assertRaises(core.UserError):
            core.check_date("2026/11/02")
        with self.assertRaises(core.UserError):
            core.check_hm("9点")
        self.assertIsNone(core.check_date(""))
        self.assertEqual(core.check_hm("09:05"), "09:05")
        self.assertEqual(core.check_hm("9:00"), "09:00")   # 宽容解析

    def test_overdue_is_display_state_only(self):
        """逾期是展示状态，不改变任务状态字段（PRD 4.3）。"""
        today = core.today_str()
        yesterday = core.date_add(today, -1)
        tid = core.task_save(self.conn, {
            "title": "逾期任务", "planned_date": yesterday, "priority": 1})
        t = core.task_get(self.conn, tid)
        self.assertEqual(t["status"], 0)          # 状态仍是未开始
        self.assertEqual(t["overdue"], 1)         # 但展示为逾期
        self.assertIn(t["id"],
                      [x["id"] for x in core.task_list(self.conn)["groups"]["overdue"]])

    def test_planned_vs_due_distinct(self):
        """计划日期与截止日期语义分开（PRD 5.1）。"""
        today = core.today_str()
        friday = core.date_add(today, 3)
        tid = core.task_save(self.conn, {
            "title": "双日期任务", "planned_date": today,
            "due_date": friday, "due_time": "18:00"})
        t = core.task_get(self.conn, tid)
        self.assertEqual(t["planned_date"], today)
        self.assertEqual(t["due_date"], friday)
        self.assertEqual(t["due_time"], "18:00")
        self.assertEqual(t["overdue"], 0)         # 截止未到，不算逾期
        # 计划日期为今天 → 进入今天列表
        self.assertIn(t["id"],
                      [x["id"] for x in core.task_list(self.conn)["groups"]["today"]])
        # 无计划日期的待办不自动进入今天（PRD 4.1）
        tid2 = core.task_save(self.conn, {"title": "无日期任务"})
        t2 = core.task_get(self.conn, tid2)
        self.assertNotIn(t2["id"],
                         [x["id"] for x in core.task_list(self.conn)["groups"]["today"]])
        self.assertIn(t2["id"],
                      [x["id"] for x in core.task_list(self.conn)["groups"]["nodate"]])
        # 有截止时间必须先有截止日期
        with self.assertRaises(core.UserError):
            core.task_save(self.conn, {"title": "坏数据", "due_time": "09:00"})

    # ---- 完成与历史 ----
    def test_complete_records_time_and_history(self):
        today = core.today_str()
        tid = core.task_save(self.conn, {
            "title": "历史任务", "planned_date": today})
        core.task_set_status(self.conn, tid, 2)
        t = core.task_get(self.conn, tid)
        self.assertEqual(t["status"], 2)
        self.assertIsNotNone(t["completed_at"])
        actions = [h["action"] for h in t["history"]]
        self.assertIn("created", actions)
        self.assertIn("status", actions)
        # 重开后完成时间清空，但历史保留
        core.task_set_status(self.conn, tid, 0)
        t2 = core.task_get(self.conn, tid)
        self.assertIsNone(t2["completed_at"])
        self.assertEqual(len(t2["history"]), len(t["history"]) + 1)

    def test_complete_task_settles_reminders(self):
        """完成待办时其未处理提醒一并处理（PRD 场景 D）。"""
        tid = core.task_save(self.conn, {"title": "带提醒的任务"})
        rid = core.reminder_save(self.conn, {
            "title": "带提醒的任务", "trigger_at": core.now() + 3600,
            "source_type": "task", "source_id": tid})
        core.task_set_status(self.conn, tid, 2)
        rems = core.reminders_all(self.conn)
        statuses = {r["id"]: r["status"] for g in rems.values() for r in g}
        self.assertEqual(statuses[rid], 2)

    # ---- 每日记录 ----
    def test_journal_reconcile_matches_and_creates(self):
        today = core.today_str()
        tid = core.task_save(self.conn, {
            "title": "回复邮件", "planned_date": today})
        res = core.journal_reconcile(self.conn, today, "- 回复邮件\n- 临时起意的事")
        self.assertEqual(res["completed"], ["回复邮件"])
        self.assertEqual(res["created"], ["临时起意的事"])
        t = core.task_get(self.conn, tid)
        self.assertEqual(t["status"], 2)
        # 幂等：重复核对不产生重复数据
        res2 = core.journal_reconcile(self.conn, today, "- 回复邮件\n- 临时起意的事")
        self.assertEqual(res2["skipped"], ["回复邮件", "临时起意的事"])
        self.assertEqual(res2["created"], [])

    def test_journal_reconcile_matches_legacy_marked_titles(self):
        """历史数据标题带列表符号时，按去符号后的标题去重，不再重复新建。"""
        today = core.today_str()
        core.task_save(self.conn, {"title": "- 旧已完成", "status": 2})
        legacy_open = core.task_save(self.conn, {
            "title": "1. 旧未完成", "planned_date": today})
        res = core.journal_reconcile(
            self.conn, today, "- 旧已完成\n- 旧未完成")
        self.assertEqual(res["created"], [])
        self.assertEqual(res["skipped"], ["旧已完成"])
        self.assertEqual(res["completed"], ["旧未完成"])
        self.assertEqual(
            core.task_get(self.conn, legacy_open)["status"], 2)
        # 同一行再核对一次仍然是幂等的
        res2 = core.journal_reconcile(
            self.conn, today, "- 旧已完成\n- 旧未完成")
        self.assertEqual(res2["created"], [])
        self.assertEqual(sorted(res2["skipped"]), ["旧已完成", "旧未完成"])

    def test_journal_reconcile_ignores_whitespace_edits(self):
        """同一行加/删空格后仍视为同一事项，不再重复新建。"""
        today = core.today_str()
        core.task_save(self.conn, {
            "title": "武安妇幼修改VD检测", "status": 2})
        res = core.journal_reconcile(
            self.conn, today, "- 武安妇幼 修改VD检测")
        self.assertEqual(res["created"], [])
        self.assertEqual(res["skipped"], ["武安妇幼 修改VD检测"])

    def test_journal_backfill_completes_on_journal_date(self):
        """补记往日日报：完成时间归到那一天，不进入今天的已完成列表。"""
        today = core.today_str()
        yesterday = core.date_add(today, -1)
        res = core.journal_reconcile(self.conn, yesterday, "- 昨天做的事")
        self.assertEqual(res["created"], ["昨天做的事"])
        d = core.today_data(self.conn, today)
        self.assertEqual([t["title"] for t in d["done"]], [])
        row = self.conn.execute(
            "SELECT planned_date, completed_at FROM biz_task WHERE title=?",
            ["昨天做的事"]).fetchone()
        self.assertEqual(row["planned_date"], yesterday)
        self.assertEqual(core.epoch_to_date(row["completed_at"]), yesterday)
        # 今天写的照旧算今天
        res2 = core.journal_reconcile(self.conn, today, "- 今天做的事")
        self.assertEqual(res2["created"], ["今天做的事"])
        d2 = core.today_data(self.conn, today)
        self.assertIn("今天做的事", [t["title"] for t in d2["done"]])

    def test_journal_written_dates_for_calendar(self):
        """日历打点：只返回写过「今日工作」的日期，空内容/只有计划不算。"""
        core.journal_save(self.conn, "2026-10-31", content="- 有内容")
        core.journal_save(self.conn, "2026-10-30", content="")
        core.journal_save(self.conn, "2026-10-29", plan="- 只有计划")
        self.assertEqual(
            core.journal_written_dates(self.conn, "2026-10"), ["2026-10-31"])

    def test_journal_manual_content_never_overwritten(self):
        """手动内容与自动汇总分开；保存手动内容不被自动汇总覆盖（PRD 4.6）。"""
        today = core.today_str()
        core.journal_save(self.conn, today, content="- 手动写的")
        j = core.journal_get(self.conn, today)
        self.assertEqual(j["content"], "- 手动写的")
        self.assertEqual(j["auto_done"], [])
        core.task_save(self.conn, {"title": "x", "status": 2})
        j2 = core.journal_get(self.conn, today)
        self.assertEqual(j2["content"], "- 手动写的")   # 自动汇总不进 content
        self.assertEqual(len(j2["auto_done"]), 1)

    def test_completed_via_tags_source(self):
        """完成来源可区分：手动勾选 / 每日总结记入（计入统计但标来源）。"""
        today = core.today_str()
        tid = core.task_save(self.conn, {
            "title": "手动完成", "planned_date": today})
        core.task_set_status(self.conn, tid, 2)
        self.assertEqual(
            core.task_get(self.conn, tid)["completed_via"], "手动")
        core.task_save(self.conn, {"title": "日报勾掉的", "planned_date": today})
        core.journal_reconcile(self.conn, today, "- 日报勾掉的\n- 日报新记的")
        done = {t["title"]: t
                for t in core.task_list(self.conn)["groups"]["done"]}
        self.assertEqual(done["日报勾掉的"]["completed_via"], "日报")
        self.assertEqual(done["日报新记的"]["completed_via"], "日报")
        self.assertEqual(done["手动完成"]["completed_via"], "手动")
        # 来源筛选
        manual = core.task_list(self.conn, "done_manual")["groups"]
        self.assertEqual([t["title"] for t in manual["done"]], ["手动完成"])
        self.assertEqual(manual["today"], [])
        journal = core.task_list(self.conn, "done_journal")["groups"]
        self.assertEqual({t["title"] for t in journal["done"]},
                         {"日报新记的", "日报勾掉的"})
        # 重新打开后来源清空
        core.task_set_status(self.conn, tid, 0)
        self.assertIsNone(core.task_get(self.conn, tid)["completed_via"])

    def test_done_source_breakdown_tiles(self):
        """今天页与工作台的今日完成拆分（勾选 / 日报）。"""
        today = core.today_str()
        tid = core.task_save(self.conn, {
            "title": "手动完成", "planned_date": today})
        core.task_set_status(self.conn, tid, 2)
        core.journal_reconcile(self.conn, today, "- 日报记的")
        d = core.today_data(self.conn, today)
        self.assertEqual(d["tiles"]["done"], 2)
        self.assertEqual(d["tiles"]["done_manual"], 1)
        self.assertEqual(d["tiles"]["done_journal"], 1)
        dash = core.dashboard(self.conn, "week")
        self.assertEqual(dash["tiles"]["done_today"], 2)
        self.assertEqual(dash["tiles"]["done_manual"], 1)
        self.assertEqual(dash["tiles"]["done_journal"], 1)

    def test_journal_reconcile_respects_deleted_records(self):
        """已删除的日报记录不会被再次核对重建（删除即用户决定）。"""
        today = core.today_str()
        content = "- 删掉的条目"
        res = core.journal_reconcile(self.conn, today, content)
        self.assertEqual(res["created"], ["删掉的条目"])
        row = self.conn.execute(
            "SELECT id FROM biz_task WHERE title='删掉的条目'").fetchone()
        core.task_delete(self.conn, row["id"])
        res2 = core.journal_reconcile(self.conn, today, content)
        self.assertEqual(res2["created"], [])
        self.assertEqual(res2["skipped"], ["删掉的条目"])
        self.assertEqual(core.task_list(self.conn)["groups"]["done"], [])

    def test_journal_future_date_does_not_create_records(self):
        """往未来日期写今日工作只存文字，不生成完成记录。"""
        tomorrow = core.date_add(core.today_str(), 1)
        res = core.journal_reconcile(self.conn, tomorrow, "- 明天才做的事")
        self.assertEqual(res["created"], [])
        self.assertEqual(res["completed"], [])
        count = self.conn.execute(
            "SELECT COUNT(*) c FROM biz_task").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_journal_same_title_on_another_day_counts_per_day(self):
        """以前某天完成过同名事项，今天日报再写仍算今天的完成（按天各记一条）。"""
        today = core.today_str()
        yesterday = core.date_add(today, -1)
        first = core.journal_reconcile(self.conn, yesterday, "- 巡检设备")
        self.assertEqual(first["created"], ["巡检设备"])
        res = core.journal_reconcile(self.conn, today, "- 巡检设备")
        self.assertEqual(res["created"], ["巡检设备"])
        d = core.today_data(self.conn, today)
        self.assertEqual([t["title"] for t in d["done"]], ["巡检设备"])
        # 同一天重复核对仍然幂等
        res2 = core.journal_reconcile(self.conn, today, "- 巡检设备")
        self.assertEqual(res2["created"], [])
        self.assertEqual(res2["skipped"], ["巡检设备"])

    def test_journal_edit_line_syncs_rename_and_remove(self):
        """日报里改写/删掉一行时，同步改名/移出当天由日报生成的记录。"""
        today = core.today_str()
        res = core.journal_reconcile(
            self.conn, today, "- 处理武安退费问题\n- 巡检设备")
        self.assertEqual(res["created"], ["处理武安退费问题", "巡检设备"])
        row = self.conn.execute(
            "SELECT id, completed_at FROM biz_task WHERE title=?",
            ["处理武安退费问题"]).fetchone()
        old_ts = row["completed_at"]
        # 改写：标题同步，完成时间保留
        res = core.journal_reconcile(
            self.conn, today, "- 处理武安镇退费问题\n- 巡检设备")
        self.assertEqual(res["renamed"], ["处理武安镇退费问题"])
        r = self.conn.execute(
            "SELECT title, completed_at, is_deleted FROM biz_task WHERE id=?",
            [row["id"]]).fetchone()
        self.assertEqual(r["title"], "处理武安镇退费问题")
        self.assertEqual(r["completed_at"], old_ts)
        self.assertEqual(r["is_deleted"], 0)
        # 删掉一行：对应记录移出，另一条保留
        res = core.journal_reconcile(
            self.conn, today, "- 处理武安镇退费问题")
        self.assertEqual(res["removed"], ["巡检设备"])
        titles = [t["title"]
                  for t in core.task_list(self.conn)["groups"]["done"]]
        self.assertEqual(titles, ["处理武安镇退费问题"])
        # 重新写回这一行：会重新生成（同步删除不算用户删除）
        res = core.journal_reconcile(
            self.conn, today, "- 处理武安镇退费问题\n- 巡检设备")
        self.assertEqual(res["created"], ["巡检设备"])

    def test_plan_written_today_shows_on_next_day_today_page(self):
        """今天写的明日计划，第二天才在「今天」页作为今日工作计划出现。"""
        today = core.today_str()
        tomorrow = core.date_add(today, 1)
        core.journal_save(self.conn, today, plan="- 明天要做 A\n- 明天要做 B")
        # 当天不显示、不生成待办、不进统计
        d = core.today_data(self.conn, today)
        self.assertEqual(d["plan"]["lines"], [])
        self.assertEqual(d["tasks"], [])
        # 第二天打开「今天」，昨天写的计划就是今天的计划
        d2 = core.today_data(self.conn, tomorrow)
        self.assertEqual(d2["plan"]["journal_date"], today)
        self.assertEqual(d2["plan"]["lines"], ["明天要做 A", "明天要做 B"])
        self.assertEqual(d2["plan"]["open_count"], 2)
        self.assertEqual(d2["tasks"], [])

    def test_plan_restore_undoes_dismiss(self):
        """已忽略的计划可以撤销，恢复为待处理。"""
        today = core.today_str()
        tomorrow = core.date_add(today, 1)
        core.journal_save(self.conn, today, plan="- 要忽略的事")
        core.plan_convert(self.conn, today, index=0, action="dismiss")
        d = core.today_data(self.conn, tomorrow)
        self.assertEqual(d["plan"]["open_count"], 0)
        self.assertEqual(d["plan"]["state"], {"0": "dismissed"})
        core.plan_restore(self.conn, today, 0)
        d2 = core.today_data(self.conn, tomorrow)
        self.assertEqual(d2["plan"]["open_count"], 1)
        self.assertEqual(d2["plan"]["state"], {})
        # 没有生成待办，撤销后依然只是计划
        self.assertEqual(d2["tasks"], [])

    def test_plan_convert_marks_work_plan_source(self):
        """明日计划转出的待办带「工作计划」来源标记，手动/收集箱来源可区分。"""
        today = core.today_str()
        core.journal_save(self.conn, today, plan="- 计划里的事")
        core.plan_convert(self.conn, today, index=0, action="task")
        planned = [t for t in core.task_list(self.conn)["groups"]["tomorrow"]
                   if t["title"] == "计划里的事"][0]
        self.assertEqual(planned["created_via"], "明日计划")
        manual = core.task_save(self.conn, {"title": "手动建的"})
        self.assertEqual(core.task_get(self.conn, manual)["created_via"], "手动")
        iid = core.inbox_save(self.conn, {"title": "收集箱转的"})
        core.inbox_convert(self.conn, {"id": iid, "kind": "task"})
        row = self.conn.execute(
            "SELECT created_via FROM biz_task WHERE title='收集箱转的'").fetchone()
        self.assertEqual(row["created_via"], "收集箱")

    def test_delete_many_soft_deletes(self):
        """批量删除：多条一起软删，未知 id 跳过，空列表报错。"""
        a = core.task_save(self.conn, {"title": "批量删 A"})
        b = core.task_save(self.conn, {"title": "批量删 B"})
        keep = core.task_save(self.conn, {"title": "保留的"})
        res = core.task_delete_many(self.conn, [a, b, b, 9999])
        self.assertEqual(res["count"], 2)
        self.assertEqual(set(res["titles"]), {"批量删 A", "批量删 B"})
        titles = [t["title"] for g in core.task_list(self.conn)["groups"].values()
                  for t in g]
        self.assertIn("保留的", titles)
        self.assertNotIn("批量删 A", titles)
        self.assertEqual(core.task_get(self.conn, keep)["title"], "保留的")
        with self.assertRaises(core.UserError):
            core.task_delete_many(self.conn, [])

    def test_plan_convert_planned_next_day(self):
        """「明日计划」转待办：planned = 日报日期 + 1 天（PRD 5.3 独立实例）。"""
        today = core.today_str()
        core.journal_save(self.conn, today, plan="- 第一件事\n- 第二件事")
        r = core.plan_convert(self.conn, today, index=0, action="task")
        self.assertEqual(r["made"], ["第一件事"])
        r2 = core.plan_convert(self.conn, today, index=0, action="dismiss")
        self.assertEqual(r2["state"]["0"], "done")     # 已转换的行不被覆盖
        # 全部转
        core.plan_convert(self.conn, today, action="task")
        tasks = core.task_list(self.conn)["groups"]["tomorrow"]
        titles = [t["title"] for t in tasks]
        self.assertIn("第二件事", titles)
        row = self.conn.execute(
            "SELECT planned_date FROM biz_task WHERE title='第二件事'").fetchone()
        self.assertEqual(row["planned_date"], core.date_add(today, 1))

    # ---- 提醒 ----
    def test_reminder_fire_and_snooze(self):
        rid = core.reminder_save(self.conn, {
            "title": "到点提醒", "trigger_at": core.now() - 10})
        self.assertEqual(core.fire_due(self.conn)[0]["id"], rid)
        # 已触发不重复触发
        self.assertEqual(core.fire_due(self.conn), [])
        # 稍后提醒：到期后再次触发
        core.reminder_snooze(self.conn, rid, minutes=10)
        self.assertEqual(core.fire_due(self.conn), [])
        past = core.now() - 5
        self.conn.execute(
            "UPDATE biz_reminder SET snoozed_until=? WHERE id=?", (past, rid))
        fired = core.fire_due(self.conn)
        self.assertEqual([r["id"] for r in fired], [rid])

    def test_notify_poll_cursor_catch_up(self):
        """启动补查：cursor=0 能拉到所有未处理已触发提醒（PRD 10.1）。"""
        core.reminder_save(self.conn, {
            "title": "错过的提醒", "trigger_at": core.now() - 7200})
        core.fire_due(self.conn)
        poll = core.notify_poll(self.conn, 0)
        self.assertEqual(poll["pending"], 1)
        self.assertEqual(len(poll["fired"]), 1)
        cursor = poll["fired"][0]["last_triggered_at"]
        poll2 = core.notify_poll(self.conn, cursor)
        self.assertEqual(poll2["fired"], [])

    # ---- 日程 ----
    def test_event_validation_and_reminder(self):
        today = core.today_str()
        with self.assertRaises(core.UserError):
            core.event_save(self.conn, {
                "title": "坏日程",
                "start_at": core.date_to_epoch(today, "15:00"),
                "end_at": core.date_to_epoch(today, "14:00")})
        # 提前量 10 分钟；开始时间取「一小时后」并限制在今天，
        # 避免用例在下午/晚上跑时提前量已被时间越过（触发时间会被夹到开始时刻）。
        start = min(core.date_to_epoch(today, "23:59"), core.now() + 3600)
        eid = core.event_save(self.conn, {
            "title": "评审会",
            "start_at": start,
            "end_at": start + 1800,
            "remind_lead": 10})
        events = core.events_range(self.conn, today, today)
        self.assertEqual(len(events), 1)
        rems = core.reminders_all(self.conn)
        lead_rem = [r for r in rems["upcoming"] if r["source_id"] == eid]
        self.assertEqual(len(lead_rem), 1)
        expected = max(start - 600, min(start, core.now()))
        self.assertLessEqual(abs(lead_rem[0]["trigger_at"] - expected), 2)
        # 全天事件
        core.event_save(self.conn, {
            "title": "全天空档", "is_all_day": 1,
            "start_date": today, "end_date": today})
        events = core.events_range(self.conn, today, today)
        self.assertTrue(any(e["is_all_day"] for e in events))
        # 删除日程 → 关联提醒失效
        core.event_delete(self.conn, eid)
        rems2 = core.reminders_all(self.conn)
        all_rems = {r["id"]: r["status"] for g in rems2.values() for r in g}
        self.assertNotIn(lead_rem[0]["id"],
                         [i for i, s in all_rems.items() if s in (0, 1)])

    # ---- 收集箱 ----
    def test_inbox_convert_keeps_created_at(self):
        """收集项转待办保留原始创建时间（PRD 4.2）。"""
        iid = core.inbox_save(self.conn, {"title": "想研究的事"})
        self.conn.execute("UPDATE biz_inbox SET created_at=? WHERE id=?",
                          (core.now() - 86400, iid))
        core.inbox_convert(self.conn, {"id": iid, "kind": "task"})
        t = self.conn.execute(
            "SELECT created_at FROM biz_task WHERE title='想研究的事'"
        ).fetchone()
        self.assertEqual(t["created_at"], core.now() - 86400)
        self.assertEqual(core.inbox_list(self.conn), [])

    # ---- 分类 ----
    def test_category_delete_reassigns(self):
        cat = core.cat_save(self.conn, {"name": "临时分类", "color": "#D98A2B"})
        core.task_save(self.conn, {"title": "挂分类的任务", "category_id": cat})
        name = core.cat_delete(self.conn, cat)
        self.assertEqual(name, "临时分类")
        t = self.conn.execute(
            "SELECT category_id FROM biz_task WHERE title='挂分类的任务'"
        ).fetchone()
        self.assertIsNone(t["category_id"])

    # ---- 备份恢复 ----
    def test_backup_restore_roundtrip(self):
        marker = "备份对照任务-%d" % core.now()
        core.task_save(self.conn, {"title": marker})
        path = db.backup_now()
        self.assertTrue(os.path.isfile(path))
        extra = "恢复后应消失-%d" % core.now()
        core.task_save(self.conn, {"title": extra})
        db.restore_from(path)
        conn2 = db.connect()
        try:
            names = {r["title"] for r in conn2.execute("SELECT title FROM biz_task")}
        finally:
            conn2.close()
        self.assertIn(marker, names)
        self.assertNotIn(extra, names)
        self.assertTrue(db.list_backups())

    # ---- 时间轴 / 驾驶舱 ----
    def test_timeline_and_dashboard(self):
        today = core.today_str()
        tid = core.task_save(self.conn, {"title": "时间轴任务"})
        core.task_set_status(self.conn, tid, 2)
        core.event_save(self.conn, {
            "title": "时间轴日程",
            "start_at": core.date_to_epoch(today, "10:00"),
            "end_at": core.date_to_epoch(today, "11:00")})
        tl = core.timeline_day(self.conn, today)
        kinds = {i["kind"] for i in tl["items"]}
        self.assertIn("done", kinds)
        self.assertIn("event", kinds)
        dash = core.dashboard(self.conn, "week")
        self.assertGreaterEqual(dash["trend"][-1]["count"], 1)

    # ---- 搜索 ----
    def test_search_across_entities(self):
        today = core.today_str()
        core.task_save(self.conn, {"title": "独特的待办关键词"})
        core.inbox_save(self.conn, {"title": "独特的收集关键词"})
        core.journal_save(self.conn, today, content="- 独特的记录关键词")
        res = core.search(self.conn, "独特的")
        types = {g["type"] for g in res["groups"]}
        self.assertEqual(types, {"task", "journal", "inbox"})
        res2 = core.search(self.conn, "独特的", "task")
        self.assertEqual([g["type"] for g in res2["groups"]], ["task"])

    def test_task_save_with_tags(self):
        """保存待办时带标签不能因事务嵌套报错，标签要真的挂上。"""
        tag_id = core.tag_add(self.conn, "客户")
        tid = core.task_save(self.conn, {"title": "带标签的待办",
                                         "tag_ids": [tag_id]})
        t = core.task_get(self.conn, tid)
        self.assertEqual([x["name"] for x in t["tags"]], ["客户"])

    def test_webui_has_no_duplicate_ids(self):
        """界面元素 id 不能重复：重复会让 getElementById 取到错的元素。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "src", "personal_workspace", "webui",
                            "index.html")
        with open(path, encoding="utf-8") as f:
            ids = re.findall(r'\bid="([^"]+)"', f.read())
        dups = sorted(k for k, v in collections.Counter(ids).items() if v > 1)
        self.assertEqual(dups, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
