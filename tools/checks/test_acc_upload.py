# -*- coding: utf-8 -*-
"""Tests for saving on ACC: lib/acc_upload.py, the folder picker in
lib/acc_file_browser.py and the silent refresh in lib/acc_auth.py.

Runs in pyRevit's IronPython, without Revit and without Autodesk: a fake
APS server answers on localhost, and no real sign-in is read or written.

    powershell -ExecutionPolicy Bypass -File tools/checks/run_ironpython.ps1 tools/checks/test_acc_upload.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib"))

import clr
clr.AddReference("System")
import System
from System.Net import HttpListener

import acc_api
import acc_auth
import acc_upload

PORT = 18765
ROOT = "http://localhost:{0}".format(PORT)
WORK = tempfile.mkdtemp(prefix="dee_acc_test_")
GOOD = "token-create"


class FakeAps(object):
    """Just enough of Data Management + OSS + the token endpoint."""

    def __init__(self):
        self.reset()
        self.listener = HttpListener()
        self.listener.Prefixes.Add(ROOT + "/")
        self.listener.Start()
        t = threading.Thread(target=self.loop)
        t.daemon = True
        t.start()

    def reset(self):
        self.folders = {"top": ("Project Files", None), "other": ("Plans", None)}
        self.items = {}             # id -> [name, folder, [storage...]]
        self.objects = {}           # storage key -> {part: bytes length}
        self.completed = []
        self.puts = []
        self.log = []
        self.fail_puts = 0
        self.forbid_create = False
        self.refreshes = []
        self.refresh_ok = True
        self.page_size = 200
        self.n = 0

    def new_id(self, prefix):
        self.n += 1
        return "{0}{1}".format(prefix, self.n)

    def loop(self):
        while True:
            try:
                ctx = self.listener.GetContext()
            except Exception:
                return
            try:
                self.handle(ctx)
            except Exception as e:
                self.reply(ctx, 500, {"error": str(e)})

    def reply(self, ctx, status, data):
        ctx.Response.StatusCode = status
        raw = System.Text.Encoding.UTF8.GetBytes(json.dumps(data))
        ctx.Response.OutputStream.Write(raw, 0, raw.Length)
        ctx.Response.Close()

    def handle(self, ctx):
        req = ctx.Request
        method, path = req.HttpMethod, req.Url.AbsolutePath
        query = req.QueryString
        self.log.append((method, path))
        stream = System.IO.MemoryStream()
        req.InputStream.CopyTo(stream)
        raw = stream.ToArray()

        if path.startswith("/s3/"):
            assert req.Headers["Authorization"] is None
            if self.fail_puts:
                self.fail_puts -= 1
                return self.reply(ctx, 503, {})
            _x, _s3, key, part = path.split("/")
            self.objects.setdefault(key, {})[int(part)] = raw.Length
            self.puts.append((key, int(part), raw.Length))
            return self.reply(ctx, 200, {})

        text = System.Text.Encoding.UTF8.GetString(raw)
        if path == "/token":
            self.refreshes.append(text)
            if not self.refresh_ok:
                return self.reply(ctx, 400, {"error": "invalid_grant"})
            return self.reply(ctx, 200, {"access_token": GOOD, "expires_in": 3599,
                                         "refresh_token": "rotated"})
        if req.Headers["Authorization"] != "Bearer " + GOOD:
            return self.reply(ctx, 401, {})
        body = json.loads(text) if text else {}

        if path.endswith("/topFolders"):
            return self.reply(ctx, 200, {"data": [
                {"id": i, "type": "folders", "attributes": {"displayName": n}}
                for i, (n, p) in self.folders.items() if p is None]})
        if path.endswith("/contents"):
            folder, kind = path.split("/")[-2], query["filter[type]"]
            if folder not in self.folders:
                return self.reply(ctx, 404, {})
            if kind == "folders":
                found = [(i, n) for i, (n, p) in sorted(self.folders.items()) if p == folder]
            else:
                found = [(i, v[0]) for i, v in sorted(self.items.items()) if v[1] == folder]
            at = int(query["page[number]"] or 0)
            links = {}
            if (at + 1) * self.page_size < len(found):
                links = {"next": {"href": "{0}{1}?filter[type]={2}&page[number]={3}".format(
                    ROOT, path, kind, at + 1)}}
            return self.reply(ctx, 200, {"links": links, "data": [
                {"id": i, "type": kind, "attributes": {"displayName": n}}
                for i, n in found[at * self.page_size:(at + 1) * self.page_size]]})

        if "/signeds3upload" in path:
            key = path.split("/")[-2]
            if method == "GET":
                first, count = int(query["firstPart"]), int(query["parts"])
                assert count <= 25
                return self.reply(ctx, 200, {
                    "uploadKey": query["uploadKey"] or "UK/" + key,
                    "urls": ["{0}/s3/{1}/{2}".format(ROOT, key, first + i)
                             for i in range(count)]})
            assert body["uploadKey"] == "UK/" + key, body
            self.completed.append(key)
            return self.reply(ctx, 200, {})

        if self.forbid_create:
            return self.reply(ctx, 403, {"errors": ["no"]})
        assert req.ContentType.startswith("application/vnd.api+json"), req.ContentType
        data = body["data"]
        if path.endswith("/folders"):
            new = self.new_id("folder")
            assert data["attributes"]["extension"]["type"] == "folders:autodesk.bim360:Folder"
            self.folders[new] = (data["attributes"]["name"],
                                 data["relationships"]["parent"]["data"]["id"])
            return self.reply(ctx, 201, {"data": {"id": new}})
        if path.endswith("/storage"):
            return self.reply(ctx, 201, {"data": {
                "id": "urn:adsk.objects:os.object:wip.dm.prod/" + self.new_id("obj")}})
        if path.endswith("/items"):
            version = body["included"][0]
            assert version["id"] == data["relationships"]["tip"]["data"]["id"]
            storage = version["relationships"]["storage"]["data"]["id"]
            assert storage.split("/")[-1] in self.completed
            self.items[self.new_id("item")] = [
                data["attributes"]["displayName"],
                data["relationships"]["parent"]["data"]["id"], [storage]]
            return self.reply(ctx, 201, {"data": {}})
        if path.endswith("/versions"):
            item = self.items[data["relationships"]["item"]["data"]["id"]]
            item[2].append(data["relationships"]["storage"]["data"]["id"])
            return self.reply(ctx, 201, {"data": {"attributes": {"versionNumber": len(item[2])}}})
        return self.reply(ctx, 404, {})


aps = FakeAps()
acc_api.BASE_URL = ROOT
acc_auth.APS_TOKEN_URL = ROOT + "/token"
acc_upload._WAIT_SECONDS = 0
CACHE = os.path.join(WORK, "token.json")
acc_auth._token_cache_path = lambda account: CACHE
acc_auth._load_config = lambda: {"client_id": "cid", "redirect_uri": ROOT + "/cb"}


def no_browser(url):
    raise AssertionError("a browser was opened")


acc_auth.webbrowser.open = no_browser


def write_cache(**data):
    with open(CACHE, "w") as f:
        json.dump(data, f)


def make_file(rel, size=1000):
    path = os.path.join(WORK, "out", rel)
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    with open(path, "wb") as f:
        f.write("x" * size)
    return path


DEST = {"account": "work", "project_id": "b.p", "project_name": "P",
        "folder_id": "top", "folder_path": "Project Files"}


class Base(unittest.TestCase):
    def setUp(self):
        aps.reset()
        acc_upload.PART_SIZE = 20 * 1024 * 1024
        shutil.rmtree(os.path.join(WORK, "out"), ignore_errors=True)
        write_cache(access_token=GOOD, expires_at=time.time() + 3000, scopes=acc_auth.SCOPES)

    def uploader(self, **kw):
        return acc_upload.Uploader(DEST, os.path.join(WORK, "out"), **kw)


class Upload(Base):
    def test_new_file_then_version(self):
        path = make_file("A_NAVIS.nwc")
        ok, detail = self.uploader().upload(path)
        self.assertTrue(ok, detail)
        self.assertIn("new file", detail)
        ok, detail = self.uploader().upload(path)
        self.assertTrue(ok, detail)
        self.assertIn("version 2", detail)
        self.assertEqual(len(aps.items), 1)
        self.assertTrue(os.path.isfile(path))

    def test_version_ignores_case_and_pages(self):
        aps.page_size = 2
        for i in range(5):
            aps.items["i{0}".format(i)] = ["F{0}.NWC".format(i), "top", ["s"]]
        ok, detail = self.uploader().upload(make_file("f4.nwc"))
        self.assertIn("version 2", detail)
        self.assertEqual(len(aps.items), 5)

    def test_parts(self):
        acc_upload.PART_SIZE = 1000
        ok, detail = self.uploader().upload(make_file("big.nwc", 30500))    # 31 parts: 2 batches
        self.assertTrue(ok, detail)
        self.assertEqual([p[1] for p in aps.puts], list(range(1, 32)))
        self.assertEqual(sum(p[2] for p in aps.puts), 30500)
        self.assertEqual(aps.puts[-1][2], 500)

    def test_part_exact_multiple(self):
        acc_upload.PART_SIZE = 1000
        ok, detail = self.uploader().upload(make_file("even.nwc", 2000))
        self.assertTrue(ok, detail)
        self.assertEqual(len(aps.puts), 2)

    def test_put_retried(self):
        aps.fail_puts = 2
        ok, detail = self.uploader().upload(make_file("r.nwc"))
        self.assertTrue(ok, detail)

    def test_failed_then_retry(self):
        aps.fail_puts = 3
        up = self.uploader(delete_after=True)
        path = make_file("r.nwc")
        ok, detail = up.upload(path)
        self.assertFalse(ok)
        self.assertIn(path, detail)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(aps.items, {})
        self.assertEqual([(d, o) for d, o, _n in up.retry()], [(detail, True)])
        self.assertEqual(up.failed, [])
        self.assertFalse(os.path.isfile(path))

    def test_empty_file(self):
        ok, detail = self.uploader().upload(make_file("e.nwc", 0))
        self.assertFalse(ok)
        self.assertIn("empty", detail)

    def test_no_permission(self):
        aps.forbid_create = True
        ok, detail = self.uploader().upload(make_file("n.nwc"))
        self.assertFalse(ok)
        self.assertIn("permission", detail)

    def test_cancel(self):
        class Bar(object):
            title = "Exporting"
            cancelled = True
        up = self.uploader()
        up.progress_bar = Bar()
        ok, detail = up.upload(make_file("c.nwc"))
        self.assertFalse(ok)
        self.assertIn("cancelled", detail)
        self.assertEqual(aps.items, {})
        self.assertEqual(up.progress_bar.title, "Exporting")

    def test_unicode_name(self):
        path = make_file(u"مبنى_NAVIS.nwc")
        ok, detail = self.uploader().upload(path)
        self.assertTrue(ok, detail)
        self.assertEqual(list(aps.items.values())[0][0], u"مبنى_NAVIS.nwc")


class Folders(Base):
    def test_subfolders_created_once(self):
        up = self.uploader()
        for rel in ("AR/L02/a.nwc", "AR/L02/b.nwc", "AR/L03/c.nwc"):
            ok, detail = up.upload(make_file(rel))
            self.assertTrue(ok, detail)
        self.assertIn("Project Files / AR / L03", detail)
        self.assertEqual(sorted(n for n, p in aps.folders.values()),
                         ["AR", "L02", "L03", "Plans", "Project Files"])
        self.uploader().upload(make_file("ar/l02/d.nwc"))
        self.assertEqual(len(aps.folders), 5)

    def test_create_reuses(self):
        one = acc_upload.create_folder("b.p", "top", "NWC", GOOD)
        self.assertEqual(acc_upload.create_folder("b.p", "top", "nwc", GOOD), one)

    def test_names(self):
        self.assertEqual(acc_upload.clean_name(' a/b:c. '), "a_b_c")
        self.assertEqual(acc_upload.clean_name(None), "")
        self.assertRaises(Exception, acc_upload.create_folder, "b.p", "top", " . ", GOOD)


class SignIn(Base):
    def token(self, **kw):
        return acc_auth.get_access_token(account="work", interactive=False, **kw)

    def test_old_login_lacks_scope(self):
        write_cache(access_token="old", expires_at=time.time() + 3000)
        self.assertEqual(self.token(), "old")
        self.assertRaises(Exception, self.token, scope=acc_auth.SCOPE_CREATE)
        ok, detail = self.uploader().upload(make_file("x.nwc"))
        self.assertFalse(ok)
        self.assertIn("not signed in", detail)

    def test_silent_refresh(self):
        acc_auth._save_token({"access_token": "old", "expires_in": 1,
                              "refresh_token": "secret-1"}, "work")
        self.assertNotIn("secret-1", open(CACHE).read())
        self.assertEqual(self.token(scope=acc_auth.SCOPE_CREATE), GOOD)
        self.assertIn("secret-1", aps.refreshes[0])
        self.assertIn("client_id=cid", aps.refreshes[0])
        self.assertEqual(acc_auth._unprotect(json.load(open(CACHE))["refresh"]), "rotated")
        self.assertEqual(self.token(), GOOD)
        self.assertEqual(len(aps.refreshes), 1)

    def test_refresh_refused(self):
        aps.refresh_ok = False
        acc_auth._save_token({"access_token": "old", "expires_in": 1,
                              "refresh_token": "used"}, "work")
        self.assertRaises(Exception, self.token)

    def test_no_cache(self):
        os.remove(CACHE)
        self.assertRaises(Exception, self.token)


# ---- the pickers, with pyRevit and Revit replaced by stand-ins
class Forms(object):
    """Answers the dialogs from a script."""
    def __init__(self):
        self.answers = []
        self.alerts = []
        self.shown = []
        forms = self

        class SelectFromList(object):
            @staticmethod
            def show(options, **kw):
                forms.shown.append(list(options))
                answer = forms.answers.pop(0)
                if answer is None:
                    return None
                return [o for o in options if o.startswith(answer)][0]

        self.SelectFromList = SelectFromList
        self.CommandSwitchWindow = SelectFromList

    def ask_for_string(self, **kw):
        return self.answers.pop(0)

    def alert(self, text, **kw):
        self.alerts.append(text)


class Anything(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return type(name, (object,), {})


fake_forms = Forms()
for module in ("Autodesk", "Autodesk.Revit", "Autodesk.Revit.DB", "Autodesk.Revit.UI",
               "dee_known_dialogs", "dee_ui"):
    sys.modules[module] = Anything(module)
pyrevit = types.ModuleType("pyrevit")
pyrevit.forms = fake_forms
sys.modules["pyrevit"] = pyrevit
import acc_file_browser as afb  # noqa: E402


class Picker(Base):
    def setUp(self):
        Base.setUp(self)
        acc_auth.get_active_account = lambda: "work"
        fake_forms.answers, fake_forms.alerts, fake_forms.shown = [], [], []

    def test_root_has_no_new_folder(self):
        fake_forms.answers = [None]
        self.assertIsNone(afb.pick_save_folder("h", "b.p", GOOD))
        self.assertEqual(fake_forms.shown[0], ["Plans", "Project Files"])

    def test_new_folder(self):
        fake_forms.answers = ["Project Files", "[+", "NWC", "[+", "2026-11-23", "[Save"]
        folder_id, path = afb.pick_save_folder("h", "b.p", GOOD)
        self.assertEqual(path, "Project Files / NWC / 2026-11-23")
        self.assertEqual(aps.folders[folder_id][0], "2026-11-23")

    def test_back_and_cancelled_name(self):
        fake_forms.answers = ["Plans", "[..", "Project Files", "[+", None, "[Save"]
        self.assertEqual(afb.pick_save_folder("h", "b.p", GOOD), ("top", "Project Files"))

    def test_create_refused(self):
        aps.forbid_create = True
        fake_forms.answers = ["Project Files", "[+", "NWC", None]
        self.assertIsNone(afb.pick_save_folder("h", "b.p", GOOD))
        self.assertIn("permission", fake_forms.alerts[0])

    def test_last(self):
        fake_forms.answers = ["Save in this folder"]
        self.assertEqual(afb.pick_save_destination(DEST), DEST)

    def test_new_inside_last(self):
        fake_forms.answers = ["Add a new", "Week 48"]
        dest = afb.pick_save_destination(DEST)
        self.assertEqual(dest["folder_path"], "Project Files / Week 48")
        self.assertEqual(aps.folders[dest["folder_id"]], ("Week 48", "top"))
        self.assertEqual(DEST["folder_id"], "top")

    def test_pick_other(self):
        afb.pick_hub = lambda token: ("h", "US", "Hub")
        afb.pick_project = lambda hub, token: ("b.q", "Q")
        fake_forms.answers = ["Pick another", "Plans", "[Save"]
        dest = afb.pick_save_destination(DEST)
        self.assertEqual((dest["project_id"], dest["folder_id"], dest["account"]),
                         ("b.q", "other", "work"))
        json.dumps(dest)


suite = unittest.TestSuite(unittest.TestLoader().loadTestsFromTestCase(c)
                           for c in (Upload, Folders, SignIn, Picker))
result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
aps.listener.Stop()
shutil.rmtree(WORK, ignore_errors=True)
print("PASS" if result.wasSuccessful() else "FAIL")
