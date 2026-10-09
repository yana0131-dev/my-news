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

## 注意点

- **サイトは公開URLです**(無料プランでは Pages は誰でも見られます)。ニュースの見出しだけで個人情報は含みませんが、URLは他人に教えない前提で使ってください。テーマの選択状況は各自のブラウザ内にしか保存されません。
- GitHub の定期実行は**数分〜十数分遅れる**ことがあります。また、公開リポジトリでは**60日間リポジトリに動きがないと定期実行が自動停止**します。止まったら Actions タブから再有効化するか、何か1コミットしてください。
- ニュースの本文は掲載せず、見出しと元記事へのリンクだけを表示します。リンクはGoogleニュース経由です。
- 手元で試すとき: `python scripts/fetch_news.py site/news.json` のあと、`cd site && python -m http.server` で `http://localhost:8000` を開きます。
