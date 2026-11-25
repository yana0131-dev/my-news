# My News(自分専用ニュースサイト)

GitHub Actions が **1時間ごと** にニュースを集め、GitHub Pages で公開します。スマホのブラウザで開いて、表示するテーマを選べます。

```
topics.json           … テーマの一覧(ここを編集してテーマを増減)
scripts/fetch_news.py … ニュース取得(Google ニュース RSS、標準ライブラリのみ)
site/                 … 公開されるサイト(index.html など)
.github/workflows/    … 1時間ごとの自動更新とデプロイ
```

## セットアップ(最初の1回だけ)

1. GitHub で新しいリポジトリを作る(例: `my-news`)。
2. このフォルダの中身をすべてそのリポジトリに push する。
   ```bash
   cd my-news
   git init -b main
   git add .
   git commit -m "first commit"
   git remote add origin https://github.com/<あなたのユーザー名>/my-news.git
   git push -u origin main
   ```
3. リポジトリの **Settings → Pages → Build and deployment → Source** を **GitHub Actions** にする。
4. **Actions** タブ → **Update news** → **Run workflow** で初回を手動実行する(以降は push 時と毎時の自動実行)。
5. 完了後、`https://<あなたのユーザー名>.github.io/my-news/` をスマホで開く。

### iPhone でアプリのように使う
Safari で開き、共有ボタン → **ホーム画面に追加**。

## テーマの選び方

- **スマホから**: 画面右上の「テーマ」を押し、表示したいテーマにチェック。選択はその端末のブラウザに保存されます。
- **テーマ自体を増やす**: `topics.json` の `topics` に追記して push します(次の更新から反映)。

```json
{ "id": "ev", "name": "EV・自動車", "type": "search", "query": "電気自動車 OR EV", "default": false }
```

| type | 使い方 |
|------|--------|
| `google_topic` | Google ニュースの定番カテゴリ(`NATION` `WORLD` `BUSINESS` `TECHNOLOGY` `SCIENCE` `HEALTH` `SPORTS` `ENTERTAINMENT`) |
| `search` | `query` にキーワード。`OR` で複数指定、`"完全一致"` も可 |
| `feed` | `url` に任意の RSS / Atom の URL |

`default: true` のテーマは、初めて開いたときに最初から選択されます。
`settings` では 1テーマあたりの最大件数(`max_items_per_topic`)と、何日前までの記事を残すか(`max_age_days`)を変えられます。

## 富山ネタ帳(番組のネタ探し用)

画面上部の切り替えで **My News / 富山ネタ帳** を行き来できます。富山ネタ帳では、次の情報をまとめて集めます。

| テーマ | 集め方 |
|------|--------|
| 県内ニュース / 地元メディア | Google・Bing ニュース検索(県内15市町村名、北日本新聞・KNB・チューリップテレビ・BBT・NHK富山) |
| 予定・先取り / 新店・新商品 / SNSで話題 | キーワード検索(「開催へ」「オープン予定」「バズ」など) |
| プレスリリース | PR TIMES・@Press・ValuePress のうち富山を含むもの |
| 富山県 報道発表 / 富山県 新着 / 富山市 新着 | 県・市の公式RSS |
| 観光イベント | とやま観光ナビのイベント一覧(`html_links` 型でページからリンクを拾う) |

画面の特別なボタン:

- **先取り** … 見出し・概要に「12月5日」などの **これから先の日付** がある記事を、開催日順に並べます(開催中のものは先頭)。
- **注目ネタ** … Gemini が「ネタ度4以上」と判定した記事だけ(下記の設定をしたときだけ表示)。
- **保存したネタ** … 記事の ☆ を押すと保存。記事が一覧から消えても、その端末に残ります。
- **SNS検索** … X・Instagram・TikTok・YouTube のよく使う検索をワンタップで開きます。X や Instagram は公式APIが有料・制限ありのため自動収集はせず、検索を開く方式にしています。検索は `topics.json` の `settings.sns_searches` で増やせます。

### Gemini でネタ度を判定する(任意)

リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で
名前 `GEMINI_API_KEY`、値に Gemini の APIキーを登録すると、富山ネタ帳の記事ごとに
**ネタ度(1〜5)・ジャンル・場所・取材の切り口** が付きます。判定済みの記事は次回以降使い回すので、1回の更新で新しく判定するのは最大60件です。
登録しなければ判定なしで動きます。モデルを変えたいときは、ワークフローの env に `GEMINI_MODEL` を追加します(既定は `gemini-flash-latest`)。

### テーマを増やす

`topics.json` に `"group": "toyama"` を付けたテーマを足すと富山ネタ帳に並びます。RSSの無い一覧ページは次のように書けます。

```json
{ "id": "t_takaoka", "name": "高岡市イベント", "type": "html_links", "group": "toyama",
  "url": "https://example.jp/event/", "link_pattern": "/event/\\d+", "source": "高岡市", "max_age_days": 60, "bing": false }
```

YouTube チャンネルの新着動画は、`"type": "feed"` で `https://www.youtube.com/feeds/videos.xml?channel_id=チャンネルID` を指定すると取れます。

## 注意点

- **サイトは公開URLです**(無料プランでは Pages は誰でも見られます)。ニュースの見出しだけで個人情報は含みませんが、URLは他人に教えない前提で使ってください。テーマの選択状況は各自のブラウザ内にしか保存されません。
- GitHub の定期実行は**数分〜十数分遅れる**ことがあります。また、公開リポジトリでは**60日間リポジトリに動きがないと定期実行が自動停止**します。止まったら Actions タブから再有効化するか、何か1コミットしてください。
- ニュースの本文は掲載せず、見出しと元記事へのリンクだけを表示します。リンクはGoogleニュース経由です。
- 手元で試すとき: `python scripts/fetch_news.py site/news.json` のあと、`cd site && python -m http.server` で `http://localhost:8000` を開きます。
