# niji-data-search

にじさんじ推し活ツール（**非公式ファンツール**）のデータ置き場です。
ANYCOLOR株式会社およびにじさんじ公式とは関係ありません。広告・有料機能はありません。

データは [Holodex](https://holodex.net/) の API から取得しています（**Powered by Holodex**）。
利用は [Holodex API License](https://docs.holodex.net/#section/LICENSE) に従い、同ライセンスの免責
（API とデータは現状のまま提供され、正確性を含め一切保証されない）が適用されます。

## 中身

- `batch/radar.py` … 他枠出演レーダーのデータを作るバッチ（`batch/holodex.py` が API の呼び出し）
- `.github/workflows/radar.yml` … 30分ごとに実行し、出力を `gh-pages` ブランチに置く
- `livers_master.csv` … ライバーマスターの控え。リポジトリ変数 `MASTER_CSV_URL`（スプレッドシートを「ウェブに公開」した CSV）があればそちらを使う

## 出力（GitHub Pages）

- `radar/index.json` … 最終更新時刻と、ライバーごとの件数・次の出演
- `radar/{channel_id}.json` … ライバー別の他枠出演（これから／過去180日）
- `ics/{channel_id}.ics` … ライバー別のカレンダー。反映に時間がかかることがあるため、ざっくり把握用
- `state.json` … 次回の差分取得に使う状態

メンバー限定配信は対象外です。取得に失敗したときは、前回のデータをそのまま出し続けます。

## 設定

- Secrets：`HOLODEX_API_KEY`（Holodex のアカウント設定で発行したキー。1アプリ1キー）
- Variables（任意）：`MASTER_CSV_URL`
- Pages：Source を `Deploy from a branch`、Branch を `gh-pages` / `(root)` にする

## テスト

```
python -m unittest discover tests
```
