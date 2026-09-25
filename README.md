# niji-data-search

にじチェッカー（にじさんじの配信スケジュール確認ツール。**非公式ファンサイト**）のデータ置き場です。
ANYCOLOR株式会社およびにじさんじ公式とは関係ありません。広告・有料機能はありません。

データは [Holodex](https://holodex.net/) の API から取得しています（**Powered by Holodex**）。
利用は [Holodex API License](https://docs.holodex.net/#section/LICENSE) に従い、同ライセンスの免責
（API とデータは現状のまま提供され、正確性を含め一切保証されない）が適用されます。

## 中身

- `batch/radar.py` … 配信の一覧（自枠・他枠）のデータを作るバッチ（`batch/holodex.py` が API の呼び出し）
- `.github/workflows/radar.yml` … 30分ごとに実行し、出力を `gh-pages` ブランチに置く
- `livers_master.csv` … ライバーマスターの控え。リポジトリ変数 `MASTER_CSV_URL`（スプレッドシートを「ウェブに公開」した CSV）があればそちらを使う

## 出力（GitHub Pages）

- `radar/index.json` … 最終更新時刻と、ライバーごとの読み・グループ（`jp`：にじさんじ〔本家・旧KR・旧ID〕／`en`：NIJISANJI EN）・色（マスターの `color` 列。`#RRGGBB` のみ）・件数（他枠・自枠）・次の他枠出演
- `radar/{channel_id}.json` … ライバー別の他枠出演（`upcoming`／`past`）と自枠（`own_upcoming`／`own_past`）。過去は180日分。各項目の `kind` は 配信（`live`）・動画（`video`）・ショート（`short`）の推定（Holodex に区別が無いため、開始時刻と長さ・題名から推定）
- `state.json` … 次回の差分取得に使う状態

YouTube の配信・動画だけが対象です（Twitch などは対象外）。メンバー限定配信も対象外です。取得に失敗したときは、前回のデータをそのまま出し続けます。
カレンダー（ICS）の出力は 2026-09-25 にやめました（以前の `ics/` はバッチが消します）。

## 設定

- Secrets：`HOLODEX_API_KEY`（Holodex のアカウント設定で発行したキー。1アプリ1キー）
- Variables（任意）：`MASTER_CSV_URL`
- Pages：Source を `Deploy from a branch`、Branch を `gh-pages` / `(root)` にする

## テスト

```
python -m unittest discover tests
```
