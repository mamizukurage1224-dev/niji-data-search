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

SHO, KAGETSU, HOST_SUB, EN, EN_LIVER, EX_KR, CN = "UCsho", "UCkagetsu", "UCsho_sub", "UCen", "UCvox", "UChayun", "UCcn"
MASTER = [
    {"channel_id": EN_LIVER, "display_name": "Vox Akuma", "branch": "EN", "channel_type": "liver", "inactive": "FALSE"},
    {"channel_id": EX_KR, "display_name": "ハ ユン", "branch": "旧KR", "channel_type": "liver", "inactive": "FALSE"},
    {"channel_id": CN, "display_name": "VR liver", "branch": "CN", "channel_type": "liver", "inactive": "FALSE"},
    {"channel_id": SHO, "display_name": "星導ショウ", "english_name": "Hoshirube Sho", "kana": "ほしるべしょう", "kana_alias": "しょう",
     "branch": "本家", "channel_type": "liver", "inactive": "FALSE", "color": "#a58cdc"},
    {"channel_id": KAGETSU, "display_name": "叢雲カゲツ", "branch": "本家", "channel_type": "liver", "inactive": "FALSE",
     "color": "red; background: url(x)"},
    {"channel_id": HOST_SUB, "display_name": "星導ショウ", "branch": "本家", "channel_type": "sub", "inactive": "FALSE"},
    {"channel_id": EN, "display_name": "", "name_holodex": "EN liver", "branch": "EN", "channel_type": "", "inactive": "FALSE"},
]


def video(vid, host, mentions, status="past", hours=-24, topic=None, duration=3600, description=None):
    start = radar.iso(radar.now_utc() + timedelta(hours=hours))
    return {"id": vid, "title": f"title {vid}", "type": "stream", "topic_id": topic, "status": status,
            "channel": {"id": host, "name": host}, "start_scheduled": start,
            "start_actual": start if status != "upcoming" else None, "available_at": start,
            "duration": duration if status == "past" else 0, "mentions": [{"id": m} for m in mentions],
            "description": description}


class FakeHolodex:
    def __init__(self, live=(), past=(), collabs=(), channels=(), missing=(), fail=False):
        self.live, self.past, self.collabs, self.channels, self.fail = list(live), list(past), list(collabs), list(channels), fail
        self.missing = list(missing)   # /videos?status=missing（非公開・削除になった動画）で返すもの

    def get(self, path, params=None, retries=3):
        if self.fail:
            raise holodex.HolodexError("テスト用の失敗")
        offset = (params or {}).get("offset", 0)
        if path == "/live":
            return self.live[offset:offset + 50]
        if path == "/videos" and (params or {}).get("status") == "missing":
            return self.missing[offset:offset + 50]
        if path == "/videos":
            return self.past[offset:offset + 50]
        if path.endswith("/collabs"):
            return self.collabs
        if path == "/channels":
            return self.channels[offset:offset + 50]
        raise AssertionError(path)


class DatedHolodex(FakeHolodex):
    """/videos の from・to・order を効かせる偽物（古い分をさかのぼる取得を確かめる）。"""

    def get(self, path, params=None, retries=3):
        if path != "/videos" or (params or {}).get("status") == "missing":
            return super().get(path, params, retries)
        p = params or {}
        items = [v for v in self.past
                 if (not p.get("from") or v["available_at"] >= p["from"]) and (not p.get("to") or v["available_at"] <= p["to"])]
        items.sort(key=lambda v: v["available_at"], reverse=p.get("order") == "desc")
        offset = p.get("offset", 0)
        return items[offset:offset + 50]


class RadarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [mock.patch.object(radar, "OUT_DIR", self.tmp.name),
                   mock.patch.object(radar, "load_master", lambda: MASTER),
                   mock.patch.object(holodex, "WAIT_SEC", 0),
                   # 続けて実行しても毎回、他事務所の枠の補いと古い分のさかのぼりをする（間引きは専用のテストで確かめる）
                   mock.patch.object(radar, "HEAVY_EVERY_MINUTES", 0)]
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
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "radar", f"{EN}.json")))   # 分類の無い EN チャンネル
        index = json.loads(self.read("radar", "index.json"))
        self.assertTrue(index["updated_at"])
        # 本家・旧KR（いまは にじさんじ）・EN のライバーを載せ、VirtuaReal は載せない
        groups = {x["channel_id"]: x["group"] for x in index["livers"]}
        self.assertEqual(groups, {KAGETSU: "jp", SHO: "jp", EX_KR: "jp", EN_LIVER: "en"})
        self.assertEqual(next(x for x in index["livers"] if x["channel_id"] == SHO)["kana"], "ほしるべしょう")
        colors = {x["channel_id"]: x["color"] for x in index["livers"]}
        self.assertEqual((colors[SHO], colors[KAGETSU]), ("#A58CDC", ""))   # #RRGGBB 以外は画面に渡さない

    def test_own_streams(self):
        fake = FakeHolodex(
            live=[video("own_up", SHO, [], status="upcoming", hours=3)],
            past=[
                video("own1", SHO, [KAGETSU], hours=-30),                # 本人の枠 → ショウの自枠・カゲツの他枠
                video("own_sub", HOST_SUB, [], hours=-20),               # サブチャンネルの枠 → ショウの自枠
                video("own_mem", SHO, [], topic="membersonly"),          # メン限 → 載らない
                video("guest1", KAGETSU, [SHO], hours=-10),              # 他枠 → 自枠には載らない
            ])
        self.run_batch(fake)
        sho = json.loads(self.read("radar", f"{SHO}.json"))
        self.assertEqual([a["id"] for a in sho["own_upcoming"]], ["own_up"])
        self.assertEqual([a["id"] for a in sho["own_past"]], ["own_sub", "own1"])   # 新しい順
        self.assertEqual({a["owner_id"] for a in sho["own_past"]}, {SHO})
        self.assertEqual([a["id"] for a in sho["past"]], ["guest1"])
        kagetsu = json.loads(self.read("radar", f"{KAGETSU}.json"))
        self.assertEqual([a["id"] for a in kagetsu["past"]], ["own1"])
        self.assertEqual(kagetsu["past"][0]["owner_id"], SHO)
        index = json.loads(self.read("radar", "index.json"))
        sho_index = next(x for x in index["livers"] if x["channel_id"] == SHO)
        self.assertEqual((sho_index["upcoming"], sho_index["own_upcoming"], sho_index["own_past"]), (0, 1, 2))

    def test_kind(self):
        def upload(vid, duration, title=None):
            v = dict(video(vid, SHO, [], duration=duration), start_scheduled=None, start_actual=None)
            return dict(v, title=title) if title else v
        self.run_batch(FakeHolodex(
            live=[video("up", SHO, [], status="upcoming", hours=3)],
            past=[video("stream", SHO, [], duration=3600),                   # 開始時刻あり → 配信
                  upload("mv", 240),                                         # 開始時刻なし → 動画
                  upload("s100", 100),                                       # 2分以下 → ショート
                  upload("s150", 150, "3分近いショート #にじさんじ"),            # ハッシュタグ付きで3分以下 → ショート
                  upload("v150", 150)]))                                     # ハッシュタグなしで2分超え → 動画
        sho = json.loads(self.read("radar", f"{SHO}.json"))
        kinds = {a["id"]: a["kind"] for a in sho["own_upcoming"] + sho["own_past"]}
        self.assertEqual(kinds, {"up": "live", "stream": "live", "mv": "video", "s100": "short",
                                 "s150": "short", "v150": "video"})
        # Holodex の分類があれば、長さより分類を使う
        upload = {"status": "past", "start_actual": None, "title": "告知PV", "has_live_info": True}
        self.assertEqual(radar.kind_of(dict(upload, topic_id="shorts", duration=170)), "short")   # 3分近くてもショート
        self.assertEqual(radar.kind_of(dict(upload, topic_id="talk", duration=69)), "video")      # 短くても別の分類なら動画
        self.assertEqual(radar.kind_of(dict(upload, topic_id=None, duration=69)), "short")        # 分類が無ければ長さで
        self.assertEqual(radar.kind_of(dict(upload, topic_id="shorts", duration=47, start_actual="2026-09-26T00:00:00Z")),
                         "live")                                                                  # #shorts の縦型の生配信は配信
        # 開始時刻を取っていない古いデータは、長さで配信か動画かを推定する
        legacy = {"status": "past", "start_actual": None, "title": "", "duration": 3600}
        self.assertEqual(radar.kind_of(legacy), "live")
        self.assertEqual(radar.kind_of(dict(legacy, duration=300)), "video")

    def test_old_state_is_refetched_with_live_info(self):
        self.run_batch(FakeHolodex())
        state = json.loads(self.read("state.json"))
        self.assertEqual(state["schema"], radar.STATE_SCHEMA)
        state.pop("schema")   # 開始時刻を取る前の形式に戻す
        with open(os.path.join(self.tmp.name, "state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)
        calls = []
        fake = FakeHolodex()
        with mock.patch.object(holodex, "get", lambda p, params=None, retries=3: calls.append((p, params)) or fake.get(p, params)):
            radar.main()
        params = next(params for p, params in calls if p == "/videos")
        self.assertIn("live_info", params["include"])
        backfill_from = radar.parse_time(params["from"])
        self.assertLess(backfill_from, radar.now_utc() - timedelta(days=radar.BACKFILL_DAYS - 1))   # 差分ではなく取り直し

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

    def test_always_on_live_is_dropped(self):
        onair = video("onair", KAGETSU, [SHO], status="live", hours=-3)
        self.run_batch(FakeHolodex(live=[
            onair,                                                                 # ふつうの配信中 → 載る
            dict(video("late", KAGETSU, [SHO], status="live", hours=-2),
                 start_scheduled=radar.iso(radar.now_utc() - timedelta(hours=48))),  # 予定より大きく遅れて開始 → 実際の開始で見るので載る
            dict(video("stuck", KAGETSU, [SHO], status="live", hours=-30),
                 start_actual=None),                                               # 実際の開始が無ければ予定で見る → 載らない
            video("station", KAGETSU, [SHO], status="live", hours=-24 * 15),      # 常時配信（開始から15日）→ 載らない
        ]))
        sho = json.loads(self.read("radar", f"{SHO}.json"))
        self.assertEqual([a["id"] for a in sho["upcoming"]], ["onair", "late"])
        index = json.loads(self.read("radar", "index.json"))
        self.assertEqual(next(x for x in index["livers"] if x["channel_id"] == SHO)["next"], onair["start_actual"])
        self.assertNotIn("station", json.loads(self.read("state.json"))["videos"])   # 状態からも落とす

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

    def test_older_past_is_backfilled_step_by_step(self):
        # 1日1本、1〜150日前と、画面に出す期間より前（200日前）
        past = [video(f"d{d}", KAGETSU, [SHO], hours=-24 * d - 1) for d in [*range(1, 151), 200]]
        fake = DatedHolodex(past=past)
        with mock.patch.object(radar, "OLDER_MAX_PAGES", 1):   # 1回にさかのぼるのは50本まで
            self.run_batch(fake)
            state = json.loads(self.read("state.json"))
            self.assertIn("d79", state["videos"])        # 30日前（初回の分）から50本さかのぼった
            self.assertNotIn("d81", state["videos"])
            self.assertEqual(state["past_from"], state["videos"]["d79"]["available_at"])
            self.run_batch(fake)                           # 次の回は続きから
            self.run_batch(fake)
            state = json.loads(self.read("state.json"))
            self.assertIn("d150", state["videos"])
            self.assertNotIn("d200", state["videos"])     # 画面に出す期間より前は取らない
            sho = json.loads(self.read("radar", f"{SHO}.json"))
            archive = json.loads(self.read("radar", "archive", f"{SHO}.json"))
            self.assertEqual(len(sho["past"]) + len(archive["past"]), 150)
            calls = []
            with mock.patch.object(holodex, "get",
                                   lambda p, params=None, retries=3: calls.append((p, params)) or fake.get(p, params)):
                radar.main()
            self.assertFalse([params for p, params in calls if p == "/videos" and params.get("to")])   # 取りきったら、もうさかのぼらない

    def test_state_without_past_from_continues_from_the_oldest(self):
        past = [video(f"d{d}", KAGETSU, [SHO], hours=-24 * d - 1) for d in range(1, 11)]
        self.run_batch(DatedHolodex(past=past))
        state = json.loads(self.read("state.json"))
        state.pop("past_from")   # さかのぼる取得を足す前の形式
        with open(os.path.join(self.tmp.name, "state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)
        calls = []
        fake = DatedHolodex(past=past)
        with mock.patch.object(holodex, "get", lambda p, params=None, retries=3: calls.append((p, params)) or fake.get(p, params)):
            radar.main()
        to = next(params["to"] for p, params in calls if p == "/videos" and params.get("to"))
        oldest = radar.parse_time(past[-1]["available_at"])
        self.assertEqual(to, radar.iso(oldest + timedelta(hours=1)))

    def test_recent_and_archive_are_split(self):
        # 直近 RECENT_DAYS 日はライバー別JSONに、それより前は archive/ に分ける
        self.run_batch(FakeHolodex(past=[video("new", KAGETSU, [SHO], hours=-24 * 10),
                                         video("old", KAGETSU, [SHO], hours=-24 * 60),
                                         video("own_old", SHO, [], hours=-24 * 90)]))
        sho = json.loads(self.read("radar", f"{SHO}.json"))
        archive = json.loads(self.read("radar", "archive", f"{SHO}.json"))
        self.assertEqual([a["id"] for a in sho["past"]], ["new"])
        self.assertEqual([a["id"] for a in archive["past"]], ["old"])
        self.assertEqual([a["id"] for a in archive["own_past"]], ["own_old"])
        self.assertEqual(sho["recent_from"], archive["to"])
        index = json.loads(self.read("radar", "index.json"))
        self.assertEqual(next(x for x in index["livers"] if x["channel_id"] == SHO)["past"], 2)   # 件数は全期間

    def test_files_of_removed_livers_are_dropped(self):
        os.makedirs(os.path.join(self.tmp.name, "radar"))
        with open(os.path.join(self.tmp.name, "radar", "UCgone.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        self.run_batch(FakeHolodex())
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "radar", "UCgone.json")))

    def test_unknown_and_ended_channels_are_reported_once(self):
        report = os.path.join(self.tmp.name, "channel_check.md")
        channels = [{"id": SHO, "name": "星導ショウ", "inactive": False},
                    {"id": "UCnew", "name": "新人|ライバー", "english_name": "Newbie", "group": "新人", "inactive": False},
                    {"id": EN_LIVER, "name": "Vox Akuma", "inactive": True}]   # マスターでは現役
        calls = []
        fake = FakeHolodex(channels=channels)
        get = lambda p, params=None, retries=3: calls.append(p) or fake.get(p, params)
        with mock.patch.object(radar, "CHANNEL_REPORT", report), mock.patch.object(holodex, "get", get):
            radar.main()
            text = open(report, encoding="utf-8").read()
            self.assertIn("UCnew", text)
            self.assertIn("新人／ライバー", text)   # 表が崩れないよう | を置き換える
            self.assertIn(EN_LIVER, text)
            self.assertNotIn(f"[{SHO}]", text)
            os.remove(report)
            calls.clear()
            radar.main()                                   # 1日たっていないので確かめない
            self.assertNotIn("/channels", calls)
            state = json.loads(self.read("state.json"))
            state["channels_checked_at"] = radar.iso(radar.now_utc() - timedelta(hours=25))
            with open(os.path.join(self.tmp.name, "state.json"), "w", encoding="utf-8") as f:
                json.dump(state, f)
            radar.main()                                   # 1日たったので確かめるが、同じ顔ぶれなので知らせない
            self.assertIn("/channels", calls)
            self.assertFalse(os.path.exists(report))

    def test_channel_check_failure_does_not_stop_the_update(self):
        fake = FakeHolodex(past=[video("p1", KAGETSU, [SHO])])
        def get(p, params=None, retries=3):
            if p == "/channels":
                raise holodex.HolodexError("テスト用の失敗")
            return fake.get(p, params)
        with mock.patch.object(radar, "CHANNEL_REPORT", os.path.join(self.tmp.name, "channel_check.md")), \
                mock.patch.object(holodex, "get", get):
            radar.main()
        self.assertEqual(json.loads(self.read("radar", f"{SHO}.json"))["past"][0]["id"], "p1")
        self.assertIsNone(json.loads(self.read("state.json"))["channels_checked_at"])   # 次の回にまた確かめる

    def test_heavy_fetches_run_every_other_time(self):
        # 15分おきの実行では、これからの配信と新しい過去分は毎回、collabs と古い分のさかのぼりは間を空けて取る
        past = [video(f"d{d}", KAGETSU, [SHO], hours=-24 * d - 1) for d in range(1, 60)]
        fake = DatedHolodex(past=past)
        calls = []
        get = lambda p, params=None, retries=3: calls.append((p, params or {})) or fake.get(p, params)
        heavy = lambda: [p for p, params in calls if p.endswith("/collabs") or (p == "/videos" and params.get("to"))]
        with mock.patch.object(radar, "HEAVY_EVERY_MINUTES", 25), mock.patch.object(holodex, "get", get):
            radar.main()
            self.assertTrue(heavy())                        # 初回は取る
            calls.clear()
            radar.main()                                    # すぐ次の回は休む
            self.assertFalse(heavy())
            self.assertIn("/live", [p for p, _ in calls])   # これからの配信は毎回取る
            state = json.loads(self.read("state.json"))
            state["heavy_at"] = radar.iso(radar.now_utc() - timedelta(minutes=26))
            with open(os.path.join(self.tmp.name, "state.json"), "w", encoding="utf-8") as f:
                json.dump(state, f)
            calls.clear()
            radar.main()                                    # 25分たてばまた取る
            self.assertTrue(heavy())

    def test_members_only_titles_are_excluded(self):
        # Holodex がメン限と分類していなくても、題名にメン限の言葉があれば載せない（自枠・他枠とも）
        self.run_batch(FakeHolodex(past=[
            dict(video("m1", SHO, []), title="【メン限】まったり雑談"),
            dict(video("m2", KAGETSU, [SHO]), title="Members Only Karaoke!"),
            dict(video("m3", SHO, []), title="メンバーシップ限定 歌枠"),
            dict(video("ok1", SHO, []), title="メンバーと遊ぶ #にじさんじ"),   # 「メンバー」だけなら載せる
            video("mt", SHO, [], topic="membersonly"),                          # これまでどおり分類でも除く
        ]))
        sho = json.loads(self.read("radar", f"{SHO}.json"))
        self.assertEqual([a["id"] for a in sho["own_past"]], ["ok1"])
        self.assertEqual(sho["past"], [])

    def test_today_lists_everyones_live_and_upcoming(self):
        # 「にじさんじ全体」：対象のライバー全員の、配信中と今日・明日の配信予定。出演する対象のライバーは cast に入れる
        self.run_batch(FakeHolodex(live=[
            video("now", SHO, [], status="live", hours=-1),
            video("soon", KAGETSU, [SHO, "UCother"], status="upcoming", hours=5),
            video("later", KAGETSU, [], status="upcoming", hours=24 * 3),               # 明後日より先は載せない
            video("cn", CN, [], status="upcoming", hours=2),                            # 対象外のライバー
            dict(video("mem", SHO, [], status="upcoming", hours=3), title="【メン限】雑談"),  # メン限
        ], past=[video("done", SHO, [])]))                                              # 終わった配信は載せない
        today = json.loads(self.read("radar", "today.json"))
        self.assertEqual([a["id"] for a in today["items"]], ["now", "soon"])
        self.assertEqual(today["items"][1]["cast"], [SHO])

    def test_missing_videos_are_dropped(self):
        # あとから非公開・削除になった動画（Holodex で status=missing）は、一覧から外す
        self.run_batch(FakeHolodex(past=[video("keep", SHO, []), video("gone", SHO, [])]))
        self.assertEqual({a["id"] for a in json.loads(self.read("radar", f"{SHO}.json"))["own_past"]}, {"keep", "gone"})
        state = json.loads(self.read("state.json"))
        state["missing_checked_at"] = radar.iso(radar.now_utc() - timedelta(hours=25))   # 1日たった
        with open(os.path.join(self.tmp.name, "state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)
        self.run_batch(FakeHolodex(missing=[dict(video("gone", SHO, []), status="missing")]))
        self.assertEqual([a["id"] for a in json.loads(self.read("radar", f"{SHO}.json"))["own_past"]], ["keep"])

    def test_credit_only_names(self):
        idents = radar.build_idents(MASTER)
        f = lambda desc, title="雑談": radar.credit_only(title, desc, [SHO], idents)
        # サムネなどのクレジットにだけ名前がある → 出演から外す（ハンドルは英語名から見つける）
        self.assertEqual(f("うおおおお\n\nサムネは毎度おなじみ\n@HoshirubeSho\n\n===\n©Konami"), {SHO})
        self.assertEqual(f("Thumbnail by @HoshirubeSho"), {SHO})
        self.assertEqual(f("【イラスト】\n@HoshirubeSho"), {SHO})
        self.assertEqual(f("サムネイラスト：星導ショウさん"), {SHO})
        # 本当の共演者は消さない：ほかの所にも名前がある・題名にある・同じ行に出演が並ぶ・見出しの次が別の項目・普通の文
        self.assertEqual(f("出演：@HoshirubeSho\nサムネ：@HoshirubeSho"), set())
        self.assertEqual(f("サムネ：@HoshirubeSho", title="【コラボ】星導ショウと雑談"), set())
        self.assertEqual(f("サムネ：@someone / 出演：@HoshirubeSho"), set())
        self.assertEqual(f("サムネは毎度おなじみ\nコラボ相手：@HoshirubeSho"), set())
        self.assertEqual(f("今日は @HoshirubeSho とイラストを描きます"), set())
        self.assertEqual(f(""), set())

    def test_thumbnail_artist_is_not_cast(self):
        calls = []
        fake = FakeHolodex(
            live=[video("up1", KAGETSU, [SHO], status="upcoming", hours=5, description="サムネは毎度おなじみ\n@HoshirubeSho")],
            past=[video("p1", KAGETSU, [SHO], description="サムネ：@HoshirubeSho"),
                  video("p2", KAGETSU, [SHO], description="出演：@HoshirubeSho")])
        with mock.patch.object(holodex, "get", lambda p, params=None, retries=3: calls.append((p, params)) or fake.get(p, params)):
            radar.main()
        self.assertTrue(all("description" in params["include"] for p, params in calls
                            if p == "/live" or (p == "/videos" and params.get("status") == "past")))
        sho = json.loads(self.read("radar", f"{SHO}.json"))
        self.assertEqual([a["id"] for a in sho["upcoming"] + sho["past"]], ["p2"])   # サムネだけの2本は他枠に出ない
        today = json.loads(self.read("radar", "today.json"))
        self.assertEqual(next(x for x in today["items"] if x["id"] == "up1")["cast"], [])
        self.assertNotIn("description", json.loads(self.read("state.json"))["videos"]["up1"])   # 概要欄は残さない

    def test_old_ics_outputs_are_removed(self):
        # カレンダー（ICS）の出力はやめたので、前回までの ics/ が残っていたら消す（公開し続けないため）
        os.makedirs(os.path.join(self.tmp.name, "ics"))
        with open(os.path.join(self.tmp.name, "ics", f"{SHO}.ics"), "w", encoding="utf-8") as f:
            f.write("BEGIN:VCALENDAR\r\n")
        self.run_batch(FakeHolodex())
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "ics")))


if __name__ == "__main__":
    unittest.main()
