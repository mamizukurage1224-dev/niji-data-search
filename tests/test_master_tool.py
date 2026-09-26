# -*- coding: utf-8 -*-
"""ライバー一覧を直す道具（batch/master_tool.py）のテスト。Holodex は呼ばない。  python -m unittest discover tests"""
import csv
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "batch"))
import holodex       # noqa: E402
import master_tool   # noqa: E402

FIELDS = ["channel_id", "name_holodex", "english_name", "group", "branch", "channel_type", "inactive",
          "comments_crawled_at", "twitter", "display_name", "kana", "kana_alias", "units", "topic_search", "color"]
SHO = "UCgZ0pH7j6c9z-pkOG3PYw1Q"
NEW = "UCnewnewnewnewnewnewnew1"


class MasterToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "livers_master.csv")
        with open(self.path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerow({**{k: "" for k in FIELDS}, "channel_id": SHO, "name_holodex": "Hoshirube Sho", "branch": "本家",
                        "channel_type": "liver", "inactive": "FALSE", "display_name": "星導ショウ", "kana": "ほしるべしょう"})
        for p in (mock.patch.object(master_tool, "PATH", self.path),
                  mock.patch.object(master_tool, "MESSAGE_FILE", os.path.join(self.tmp.name, "msg.txt")),
                  mock.patch.dict(os.environ, {"HOLODEX_API_KEY": "", "GITHUB_STEP_SUMMARY": ""})):
            p.start()
            self.addCleanup(p.stop)

    def rows(self):
        with open(self.path, encoding="utf-8-sig", newline="") as f:
            return {r["channel_id"]: r for r in csv.DictReader(f)}

    def message(self):
        with open(master_tool.MESSAGE_FILE, encoding="utf-8") as f:
            return f.read().strip()

    def test_add_new_liver(self):
        master_tool.main(["add", "--channel-id", NEW, "--name", "新人ライバー", "--kana", "シンジンライバー",
                          "--alias", "しんじん", "--color", "#a58cdc"])
        row = self.rows()[NEW]
        self.assertEqual((row["display_name"], row["kana"], row["kana_alias"], row["color"]),
                         ("新人ライバー", "しんじんらいばー", "しんじん", "#A58CDC"))   # 読みはひらがなに、色は大文字に
        self.assertEqual((row["branch"], row["channel_type"], row["inactive"]), ("本家", "liver", "FALSE"))
        self.assertEqual(self.message(), "ライバー一覧：新人ライバー を追加")
        self.assertIn(SHO, self.rows())   # ほかの行はそのまま

    def test_add_uses_holodex_and_rejects_other_orgs(self):
        info = {"name": "New Liver【NIJISANJI EN】", "english_name": "New Liver", "group": "EN", "twitter": "newliver",
                "org": "Nijisanji"}
        with mock.patch.dict(os.environ, {"HOLODEX_API_KEY": "dummy"}), mock.patch.object(holodex, "get", lambda p, params=None: info):
            master_tool.main(["add", "--channel-id", NEW, "--group", "en", "--kana", "ニュー・ライバー"])
        row = self.rows()[NEW]
        self.assertEqual((row["display_name"], row["branch"], row["kana"], row["twitter"]),
                         ("New Liver【NIJISANJI EN】", "EN", "ニュー・ライバー", "newliver"))   # EN の読みはカタカナのまま
        other = {**info, "org": "Hololive"}
        with mock.patch.dict(os.environ, {"HOLODEX_API_KEY": "dummy"}), mock.patch.object(holodex, "get", lambda p, params=None: other), \
                self.assertRaises(SystemExit):
            master_tool.main(["add", "--channel-id", "UCotherotherotherother12", "--name", "x", "--kana", "えっくす"])
        self.assertNotIn("UCotherotherotherother12", self.rows())

    def test_retire_and_edit(self):
        master_tool.main(["retire", "--channel-id", SHO])
        self.assertEqual(self.rows()[SHO]["inactive"], "TRUE")
        master_tool.main(["edit", "--channel-id", SHO, "--color", "#123abc"])
        self.assertEqual(self.rows()[SHO]["color"], "#123ABC")
        self.assertEqual(self.message(), "ライバー一覧：星導ショウ のライバーカラーを直す")

    def test_mistakes_change_nothing(self):
        before = open(self.path, encoding="utf-8-sig").read()
        for argv in (["add", "--channel-id", "UCshort", "--name", "x", "--kana", "えっくす"],   # IDの形が違う
                     ["add", "--channel-id", NEW, "--name", "x"],                             # 読みが無い
                     ["add", "--channel-id", NEW, "--name", "x", "--kana", "えっくす", "--color", "red"],
                     ["retire", "--channel-id", NEW],                                         # 一覧に無い
                     ["edit", "--channel-id", SHO]):                                          # 直す欄が無い
            with self.assertRaises(SystemExit):
                master_tool.main(argv)
        self.assertEqual(open(self.path, encoding="utf-8-sig").read(), before)

    def test_from_env_uses_japanese_choices(self):
        env = {"ACTION": "卒業・活動終了にする", "CHANNEL_ID": SHO, "NAME": "", "KANA": "", "ALIAS": "", "COLOR": "",
               "GROUP": "にじさんじ"}
        with mock.patch.dict(os.environ, env):
            master_tool.main(["--from-env"])
        self.assertEqual(self.rows()[SHO]["inactive"], "TRUE")


if __name__ == "__main__":
    unittest.main()
