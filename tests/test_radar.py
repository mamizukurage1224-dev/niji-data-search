# -*- coding: utf-8 -*-
"""他枠出演レーダーのテスト。Holodex は呼ばず、偽のデータで確かめる。  python -m unittest discover tests"""
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "batch"))
import holodex  # noqa: E402
import radar    # noqa: E402

SHO, KAGETSU, HOST_SUB, EN = "UCsho", "UCkagetsu", "UCsho_sub", "UCen"
MASTER = [
    {"channel_id": SHO, "display_name": "星導ショウ", "branch": "本家", "channel_type": "liver", "inactive": "FALSE"},
    {"channel_id": KAGETSU, "display_name": "叢雲カゲツ", "branch": "本家", "channel_type": "liver", "inactive": "FALSE"},
    {"channel_id": HOST_SUB, "display_name": "星導ショウ", "branch": "本家", "channel_type": "sub", "inactive": "FALSE"},
    {"channel_id": EN, "display_name": "", "name_holodex": "EN liver", "branch": "EN", "channel_type": "", "inactive": "FALSE"},
]


def video(vid, host, mentions, status="past", hours=-24, topic=None, duration=3600):
    start = radar.iso(radar.now_utc() + timedelta(hours=hours))
    return {"id": vid, "title": f"title {vid}", "type": "stream", "topic_id": topic, "status": status,
            "channel": {"id": host, "name": host}, "start_scheduled": start,
            "start_actual": start if status != "upcoming" else None, "available_at": start,
            "duration": duration if status == "past" else 0, "mentions": [{"id": m} for m in mentions]}


class FakeHolodex:
    def __init__(self, live=(), past=(), collabs=(), fail=False):
        self.live, self.past, self.collabs, self.fail = list(live), list(past), list(collabs), fail

    def get(self, path, params=None, retries=3):
        if self.fail:
            raise holodex.HolodexError("テスト用の失敗")
        offset = (params or {}).get("offset", 0)
        if path == "/live":
            return self.live[offset:offset + 50]
        if path == "/videos":
            return self.past[offset:offset + 50]
        if path.endswith("/collabs"):
            return self.collabs
        raise AssertionError(path)


class RadarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [mock.patch.object(radar, "OUT_DIR", self.tmp.name),
                   mock.patch.object(radar, "load_master", lambda: MASTER),
                   mock.patch.object(holodex, "WAIT_SEC", 0)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_batch(self, fake):
        with mock.patch.object(holodex, "get", fake.get):
            radar.main()

    def read(self, *parts):
        with open(os.path.join(self.tmp.name, *parts), encoding="utf-8", newline="") as f:
            return f.read()

    def test_appearances_and_exclusions(self):
        fake = FakeHolodex(
            live=[video("up1", KAGETSU, [SHO], status="upcoming", hours=5)],
            past=[
                video("p1", KAGETSU, [SHO]),                              # 他枠 → 載る
                video("p2", HOST_SUB, [SHO, KAGETSU]),                    # 自分のサブ枠 → ショウには載らない
                video("p3", KAGETSU, [SHO], topic="membersonly"),         # メン限 → 載らない
                video("p4", KAGETSU, [SHO], status="missing"),            # 消えた動画 → 載らない
            ],
            collabs=[video("c1", "UCother_org", [SHO], hours=-48)],      # 他事務所の枠 → 載る
        )
        self.run_batch(fake)
        sho = json.loads(self.read("radar", f"{SHO}.json"))
        self.assertEqual([a["id"] for a in sho["upcoming"]], ["up1"])
        self.assertEqual([a["id"] for a in sho["past"]], ["p1", "c1"])
        kagetsu = json.loads(self.read("radar", f"{KAGETSU}.json"))
        self.assertEqual([a["id"] for a in kagetsu["past"]], ["p2"])   # ショウのサブ枠はカゲツには他枠
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "radar", f"{EN}.json")))
        index = json.loads(self.read("radar", "index.json"))
        self.assertTrue(index["updated_at"])
        self.assertEqual(sorted(x["channel_id"] for x in index["livers"]), [KAGETSU, SHO])

    def test_cancelled_upcoming_is_removed(self):
        self.run_batch(FakeHolodex(live=[video("up1", KAGETSU, [SHO], status="upcoming", hours=5)]))
        self.run_batch(FakeHolodex(live=[]))   # 次の回で予定が消えた
        sho = json.loads(self.read("radar", f"{SHO}.json"))
        self.assertEqual(sho["upcoming"], [])

    def test_far_future_waiting_room_is_dropped(self):
        self.run_batch(FakeHolodex(collabs=[video("wait", "UCother_org", [SHO], status="upcoming", hours=24 * 200),
                                            video("soon", "UCother_org", [SHO], status="upcoming", hours=24)]))
        sho = json.loads(self.read("radar", f"{SHO}.json"))
        self.assertEqual([a["id"] for a in sho["upcoming"]], ["soon"])

    def test_failure_keeps_previous_outputs(self):
        self.run_batch(FakeHolodex(past=[video("p1", KAGETSU, [SHO])]))
        before = self.read("radar", f"{SHO}.json"), self.read("state.json")
        with self.assertRaises(SystemExit) as cm:
            self.run_batch(FakeHolodex(fail=True))
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual((self.read("radar", f"{SHO}.json"), self.read("state.json")), before)

    def test_second_run_fetches_only_the_difference(self):
        self.run_batch(FakeHolodex())
        state = json.loads(self.read("state.json"))
        calls = []
        fake = FakeHolodex()
        with mock.patch.object(holodex, "get", lambda p, params=None, retries=3: calls.append((p, params)) or fake.get(p, params)):
            radar.main()
        past_from = next(params["from"] for p, params in calls if p == "/videos")
        expected = radar.parse_time(state["last_past_fetch"]) - timedelta(hours=radar.PAST_OVERLAP_HOURS)
        self.assertEqual(past_from, radar.iso(expected))

    def test_truncated_past_fetch_continues_next_time(self):
        past = [video(f"p{i}", KAGETSU, [], hours=-500 + i * 0.1) for i in range(120)]
        with mock.patch.object(radar, "PAST_MAX_PAGES", 2):
            self.run_batch(FakeHolodex(past=past))
        state = json.loads(self.read("state.json"))
        self.assertEqual(state["last_past_fetch"], past[99]["available_at"])   # 100件目までで止めた
        self.assertNotEqual(state["last_past_fetch"], state["updated_at"])

    def test_ics(self):
        long_title = "とても長いタイトル" * 10 + ",;"
        self.run_batch(FakeHolodex(live=[dict(video("up1", KAGETSU, [SHO], status="upcoming", hours=5),
                                             title=long_title)]))
        ics = self.read("ics", f"{SHO}.ics")
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("UID:up1@niji-oshikatsu-tools", ics)
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)
        unfolded = ics.replace("\r\n ", "")
        self.assertIn("\\,\\;", unfolded)
        self.assertIn("Powered by Holodex", unfolded)


if __name__ == "__main__":
    unittest.main()
