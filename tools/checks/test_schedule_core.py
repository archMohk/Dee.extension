# -*- coding: utf-8 -*-
"""Tests for lib/dee_schedule_core.py (DeeSchedule's store, timing and
PC-to-PC transfer). No Revit needed:

    python tools/checks/test_schedule_core.py

Uses a throwaway folder (DEE_SCHEDULE_DIR), never this PC's real tasks.
"""
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib"))
import dee_schedule_core as core  # noqa: E402

D = datetime.datetime


def trig(kind, start, **kw):
    t = {"type": kind, "start": start}
    t.update(kw)
    return t


class Timing(unittest.TestCase):
    def test_once(self):
        t = trig("once", "2030-01-05 09:00:00")
        self.assertEqual(core.next_after(t, D(2030, 1, 1)), D(2030, 1, 5, 9))
        self.assertIsNone(core.next_after(t, D(2030, 1, 5, 9)))

    def test_interval(self):
        t = trig("interval", "2030-01-01 08:00:00", minutes=30)
        self.assertEqual(core.next_after(t, D(2029, 12, 31)), D(2030, 1, 1, 8))
        self.assertEqual(core.next_after(t, D(2030, 1, 1, 8)), D(2030, 1, 1, 8, 30))
        self.assertEqual(core.next_after(t, D(2030, 1, 3, 8, 10)), D(2030, 1, 3, 8, 30))

    def test_daily(self):
        t = trig("daily", "2030-01-01 21:30:00")
        self.assertEqual(core.next_after(t, D(2030, 1, 1, 12)), D(2030, 1, 1, 21, 30))
        self.assertEqual(core.next_after(t, D(2030, 1, 1, 21, 30)), D(2030, 1, 2, 21, 30))
        self.assertEqual(core.next_after(t, D(2029, 5, 1, 23)), D(2030, 1, 1, 21, 30))

    def test_weekly(self):
        # 2030-01-01 is a Tuesday
        t = trig("weekly", "2030-01-01 09:00:00", days=[0, 3])
        self.assertEqual(core.next_after(t, D(2030, 1, 1, 10)), D(2030, 1, 3, 9))
        self.assertEqual(core.next_after(t, D(2030, 1, 3, 9)), D(2030, 1, 7, 9))
        one = trig("weekly", "2030-01-01 09:00:00", days=[1])
        self.assertEqual(core.next_after(one, D(2030, 1, 1, 9)), D(2030, 1, 8, 9))

    def test_monthly_short_month(self):
        t = trig("monthly", "2030-01-31 18:00:00", day=31)
        self.assertEqual(core.next_after(t, D(2030, 1, 31, 18)), D(2030, 2, 28, 18))
        self.assertEqual(core.next_after(t, D(2030, 2, 28, 18)), D(2030, 3, 31, 18))
        self.assertEqual(core.next_after(t, D(2030, 12, 31, 19)), D(2031, 1, 31, 18))

    def test_end_date(self):
        t = trig("daily", "2030-01-01 09:00:00", end="2030-01-02")
        self.assertEqual(core.next_after(t, D(2030, 1, 1, 9)), D(2030, 1, 2, 9))
        self.assertIsNone(core.next_after(t, D(2030, 1, 2, 9)))

    def test_bad_triggers(self):
        for bad in (None, {}, trig("x", "2030-01-01"), trig("daily", "rubbish"),
                    trig("interval", "2030-01-01", minutes=1),
                    trig("weekly", "2030-01-01", days=[]),
                    trig("weekly", "2030-01-01", days=[9]),
                    trig("monthly", "2030-01-01", day=40),
                    trig("daily", "2030-01-02", end="2030-01-01")):
            self.assertRaises(core.ScheduleError, core.check_trigger, bad)

    def test_always_moves_forward(self):
        after = D(2030, 2, 27, 23, 59)
        for t in (trig("interval", "2029-01-01 00:07:00", minutes=45),
                  trig("daily", "2029-01-01 00:00:00"),
                  trig("weekly", "2029-01-01 00:00:00", days=[6]),
                  trig("monthly", "2029-01-01 00:00:00", day=30)):
            at = after
            for _i in range(100):
                nxt = core.next_after(t, at)
                self.assertTrue(nxt > at, t)
                at = nxt

    def test_parse_time(self):
        self.assertEqual(core.parse_time("9:05"), (9, 5))
        self.assertEqual(core.parse_time("21"), (21, 0))
        for bad in ("", "24:00", "9:60", "x", "9:0:0", None):
            self.assertIsNone(core.parse_time(bad))

    def test_describe(self):
        self.assertIn("hour", core.describe_trigger(trig("interval", "2030-01-01 08:00", minutes=60)))
        self.assertIn("Mon, Thu", core.describe_trigger(trig("weekly", "2030-01-01 08:00", days=[3, 0])))


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.environ["DEE_SCHEDULE_DIR"] = self.dir

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        os.environ.pop("DEE_SCHEDULE_DIR", None)

    def add(self, name="T", trigger=None, **kw):
        kw.setdefault("sources", [{"kind": "file", "path": r"C:\m\a.rvt", "label": "a"}])
        kw.setdefault("payload", {"sheets": ["u1"]})
        tool = kw.pop("tool", "DeePrinter")
        return core.add_task(core.new_task(
            tool, name, trigger or trig("daily", "2020-01-01 09:00:00"), **kw))

    def make_due(self, task_id, minutes=1):
        def fn(data):
            core.find(data, task_id)["next_run"] = core.fmt(
                core.now() - datetime.timedelta(minutes=minutes))
        core.mutate(fn)

    def due_ids(self, year="2024"):
        return [t["id"] for t in core.due_tasks(core.load(), core.now(), year)]

    def test_add_and_reload(self):
        t = self.add(name=u"\u0645\u0634\u0631\u0648\u0639")
        back = core.get_task(t["id"])
        self.assertEqual(back["name"], u"\u0645\u0634\u0631\u0648\u0639")
        self.assertTrue(core.parse(back["next_run"]) > core.now())

    def test_past_once_refused(self):
        self.assertRaises(core.ScheduleError, self.add, trigger=trig("once", "2020-01-01 09:00"))

    def test_unknown_tool_refused(self):
        self.assertRaises(core.ScheduleError, self.add, tool="Nope")

    def test_due_only_when_active(self):
        t = self.add()
        self.assertEqual(self.due_ids(), [])
        self.make_due(t["id"])
        self.assertEqual(self.due_ids(), [t["id"]])
        core.pause(t["id"])
        self.assertEqual(self.due_ids(), [])
        core.resume(t["id"])
        self.assertEqual(self.due_ids(), [])          # re-planned from now
        self.assertEqual(core.get_task(t["id"])["state"], "active")

    def test_master_switch_and_year(self):
        t = self.add(revit_year="2024")
        self.make_due(t["id"])
        self.assertEqual(self.due_ids("2026"), [])
        self.assertEqual(self.due_ids("2024"), [t["id"]])
        core.save_settings(enabled=False)
        self.assertEqual(self.due_ids("2024"), [])

    def test_stop_and_resume(self):
        t = self.add()
        core.stop(t["id"])
        back = core.get_task(t["id"])
        self.assertEqual((back["state"], back["next_run"]), ("stopped", ""))
        self.assertFalse(core.stop_requested(t["id"]))
        core.resume(t["id"])
        self.assertTrue(core.get_task(t["id"])["next_run"])

    def test_stop_while_running(self):
        t = self.add()
        lock = core.RunLock(t["id"])
        self.assertTrue(lock.acquired)
        self.assertFalse(core.RunLock(t["id"]).acquired)
        self.assertEqual(core.status_text(t), "Running")
        core.stop(t["id"])
        self.assertTrue(core.stop_requested(t["id"]))
        self.assertEqual(core.status_text(t), "Stopping...")
        lock.release()
        core.end_run(t["id"], "cancelled", "x")
        self.assertFalse(core.stop_requested(t["id"]))
        self.assertEqual(core.get_task(t["id"])["state"], "stopped")

    def test_delete_while_running(self):
        t = self.add()
        lock = core.RunLock(t["id"])
        self.assertTrue(core.delete(t["id"]))
        self.assertTrue(core.stop_requested(t["id"]))
        lock.release()
        core.end_run(t["id"], "cancelled", "x")        # must not bring it back
        self.assertIsNone(core.get_task(t["id"]))

    def test_run_plans_next_before_working(self):
        t = self.add()
        self.make_due(t["id"])
        self.assertIsNotNone(core.begin_run(t["id"]))
        self.assertTrue(core.parse(core.get_task(t["id"])["next_run"]) > core.now())
        self.assertIsNone(core.begin_run(t["id"]))     # second Revit: not due any more
        core.end_run(t["id"], "failed", "boom")
        back = core.get_task(t["id"])
        self.assertEqual((back["run_count"], back["fail_count"], len(back["history"])), (1, 1, 1))

    def test_once_finishes(self):
        start = core.fmt(core.now() + datetime.timedelta(hours=1))
        t = self.add(trigger=trig("once", start))

        def past(data):
            core.find(data, t["id"])["trigger"]["start"] = core.fmt(
                core.now() - datetime.timedelta(minutes=1))
        core.mutate(past)
        self.make_due(t["id"])
        core.begin_run(t["id"])
        self.assertEqual(core.get_task(t["id"])["state"], "finished")
        core.end_run(t["id"], "ok", "")
        self.assertRaises(core.ScheduleError, core.resume, t["id"])
        core.update_task(t["id"], trigger=trig("daily", "2020-01-01 10:00:00"))
        self.assertEqual(core.get_task(t["id"])["state"], "active")

    def test_manual_run_keeps_schedule(self):
        t = self.add()
        core.pause(t["id"])
        before = core.get_task(t["id"])["next_run"]
        core.begin_run(t["id"], manual=True)
        core.end_run(t["id"], "ok", "")
        back = core.get_task(t["id"])
        self.assertEqual((back["state"], back["next_run"]), ("paused", before))

    def test_missed_policy(self):
        run = self.add(run_missed=True)
        skip = self.add(run_missed=False)
        for t in (run, skip):
            self.make_due(t["id"], minutes=120)
        core.roll_missed(core.now(), "2024")
        self.assertEqual(self.due_ids(), [run["id"]])
        self.assertEqual(core.get_task(skip["id"])["history"][-1]["status"], "missed")

    def test_crashed_run_shows_interrupted(self):
        t = self.add()
        core.begin_run(t["id"], manual=True)
        self.assertIn("Interrupted", core.result_text(core.get_task(t["id"])))

    def test_history_limit(self):
        t = self.add()
        for i in range(core.HISTORY_LIMIT + 10):
            core.end_run(t["id"], "ok", str(i))
        back = core.get_task(t["id"])
        self.assertEqual(len(back["history"]), core.HISTORY_LIMIT)
        self.assertEqual(back["history"][-1]["message"], str(core.HISTORY_LIMIT + 9))

    def test_damaged_file_is_kept(self):
        self.add()
        with io.open(core.store_path(), "w", encoding="utf-8") as f:
            f.write(u"{ half a fi")
        core.load()
        self.assertTrue([n for n in os.listdir(self.dir) if "damaged" in n])
        self.add()
        self.assertTrue(core.tasks())

    def test_edit_refuses_rubbish(self):
        t = self.add()
        self.assertRaises(core.ScheduleError, core.update_task, t["id"],
                          trigger=trig("weekly", "2030-01-01", days=[]))
        self.assertEqual(core.get_task(t["id"])["trigger"]["type"], "daily")

    def test_logs(self):
        t = self.add()
        path = core.write_log(t["id"], [u"caf\u00e9", u"b"])
        self.assertEqual(core.last_log(t["id"]), path)


class Transfer(Store):
    def setUp(self):
        Store.setUp(self)
        self.env = dict(os.environ)
        os.environ["USERPROFILE"] = r"C:\Users\Old"
        os.environ["OneDrive"] = r"C:\Users\Old\OneDrive"
        for k in ("OneDriveCommercial", "OneDriveConsumer"):
            os.environ.pop(k, None)
        self.file = os.path.join(self.dir, "export.json")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        Store.tearDown(self)

    def export(self):
        core.export_to_file(self.file, core.tasks())

    def other_pc(self):
        """A second PC: empty store, different user name."""
        os.environ["DEE_SCHEDULE_DIR"] = os.path.join(self.dir, "pc2")
        os.environ["USERPROFILE"] = r"D:\Users\New"
        os.environ["OneDrive"] = r"E:\OneDrive"

    def test_round_trip(self):
        self.add(name="A", output=r"C:\Users\Old\OneDrive\Out",
                 payload={"dicdlg": {"printfilepath": r"C:\Users\Old\Docs\x"}, "n": [1, True]},
                 sources=[{"kind": "file", "path": r"C:\Users\Oldest\a.rvt", "label": "a"}])
        core.end_run(core.tasks()[0]["id"], "ok", "")
        self.export()
        self.assertNotIn("Users\\\\Old\\\\", io.open(self.file, encoding="utf-8").read())
        self.other_pc()
        got, sett, problems = core.read_export(self.file)
        self.assertEqual((len(got), problems), (1, []))
        self.assertEqual(sett["warn_seconds"], 30)
        self.assertEqual(core.import_tasks(got)["added"], 1)
        t = core.tasks()[0]
        self.assertEqual(t["output"], r"E:\OneDrive\Out")
        self.assertEqual(t["payload"]["dicdlg"]["printfilepath"], r"D:\Users\New\Docs\x")
        # a folder that merely starts with the same letters is not touched
        self.assertEqual(t["sources"][0]["path"], r"C:\Users\Oldest\a.rvt")
        self.assertEqual(t["payload"]["n"], [1, True])
        self.assertEqual((t["state"], t["history"], t["run_count"]), ("paused", [], 0))
        self.assertTrue(t["next_run"])

    def test_start_keeps_states(self):
        self.add(name="A")
        stopped = self.add(name="B")
        core.stop(stopped["id"])
        self.export()
        self.other_pc()
        got, _s, _p = core.read_export(self.file)
        core.import_tasks(got, start=True)
        self.assertEqual(dict((t["name"], t["state"]) for t in core.tasks()),
                         {"A": "active", "B": "stopped"})

    def test_duplicates(self):
        self.add(name="A")
        self.export()
        got, _s, _p = core.read_export(self.file)
        self.assertEqual(core.import_tasks(got, "skip")["skipped"], 1)
        self.assertEqual(core.import_tasks(got, "replace")["replaced"], 1)
        self.assertEqual(len(core.tasks()), 1)
        self.assertEqual(core.import_tasks(got, "copy")["added"], 1)
        self.assertEqual(len(core.tasks()), 2)
        self.assertEqual(len(set(t["id"] for t in core.tasks())), 2)

    def test_expired_once_imports_as_finished(self):
        t = self.add(trigger=trig("once", core.fmt(core.now() + datetime.timedelta(hours=1))))
        self.export()
        data = json.load(io.open(self.file, encoding="utf-8"))
        data["tasks"][0]["trigger"]["start"] = "2020-01-01 09:00:00"
        with io.open(self.file, "w", encoding="utf-8") as f:
            f.write(json.dumps(data))
        self.other_pc()
        got, _s, _p = core.read_export(self.file)
        self.assertEqual(core.import_tasks(got, start=True)["finished"], 1)
        self.assertEqual(core.tasks()[0]["state"], "finished")

    def test_bad_files(self):
        for text in (u"not json", u"[]", u"{}", u'{"format":"x","tasks":[]}'):
            with io.open(self.file, "w", encoding="utf-8") as f:
                f.write(text)
            self.assertRaises(core.ScheduleError, core.read_export, self.file)

    def test_bad_task_is_reported_not_imported(self):
        self.add(name="A")
        self.export()
        data = json.load(io.open(self.file, encoding="utf-8"))
        data["tasks"] += [{"name": "evil", "tool": "os"}, 5]
        data["settings"]["surprise"] = 1
        with io.open(self.file, "w", encoding="utf-8") as f:
            f.write(json.dumps(data))
        got, sett, problems = core.read_export(self.file)
        self.assertEqual((len(got), len(problems)), (1, 2))
        self.assertNotIn("surprise", sett)

    def test_relink(self):
        self.add(name="A", output=r"Q:\gone\out",
                 sources=[{"kind": "file", "path": r"Q:\gone\models\a.rvt", "label": "a"}])
        self.assertTrue(core.missing_paths(core.tasks()[0]))
        self.assertEqual(core.missing_folders(core.tasks()), [r"Q:\gone\models", r"Q:\gone\out"])
        self.assertEqual(core.relink(r"Q:\gone", self.dir), 1)
        t = core.tasks()[0]
        self.assertEqual(t["output"], os.path.join(self.dir, "out"))
        self.assertEqual(core.missing_paths(t)[0][0], "model")
        with io.open(os.path.join(self.dir, "models.txt"), "w") as f:
            f.write(u"")
        os.makedirs(os.path.join(self.dir, "models"))
        io.open(os.path.join(self.dir, "models", "a.rvt"), "w").close()
        self.assertEqual(core.missing_paths(t), [])

    def test_cloud_and_rsn_never_missing(self):
        t = self.add(sources=[{"kind": "cloud", "region": "US", "project": "p", "model": "m"},
                              {"kind": "file", "path": "RSN://srv/a.rvt"}])
        self.assertEqual(core.missing_paths(t), [])


if __name__ == "__main__":
    unittest.main()
