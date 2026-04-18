# エルメス ミニリンディ 在庫通知 — セットアップ手順

## 1. Python 環境の準備

```bash
cd hermes-notify
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Gmail アプリパスワードの取得

1. Google アカウント → **セキュリティ**
2. **2段階認証プロセス**を有効化（必須）
3. 2段階認証ページ下部 → **アプリパスワード**
4. アプリ名を「Hermes Notify」などと入力して生成
5. 表示された 16桁のパスワードをコピー

## 3. 設定ファイルの作成

```bash
cp .env.example .env
```

`.env` を開いて、アプリパスワードを貼り付け：
```
GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
```

`config.yaml` を開いて Gmail アドレスを設定：
```yaml
gmail:
  sender: "あなたのアドレス@gmail.com"
  recipient: "通知先@gmail.com"    # スマホのキャリアメールでも可
```

## 4. 動作確認（スキャンモード）

初回は `--scan` オプションで1回だけ実行し、セレクタが正しいか確認します：

```bash
python checker.py --scan
```

- `page_<ラベル>.html` — 取得したページのHTML
- `monitor.log` — 検出した商品数とログ

商品が検出されていれば OK。検出されない場合は `config.yaml` の  
`product_selector` / `name_selector` を `page.html` を見て調整してください。

## 5. 本番起動

```bash
python checker.py
```

デフォルトは 5分間隔でチェックします。  
`config.yaml` の `interval_minutes` で変更可能（最小5分を推奨）。

## 6. バックグラウンドで常時起動（Mac）

ターミナルを閉じても動かし続けたい場合：

```bash
nohup python checker.py > /dev/null 2>&1 &
echo $! > checker.pid    # プロセスIDを保存
```

停止するには：
```bash
kill $(cat checker.pid)
```

---

## キャリアメールアドレス形式（recipient に設定）

| キャリア | アドレス形式 |
|---------|------------|
| Docomo  | 090xxxxxxxx@docomo.ne.jp |
| au      | 090xxxxxxxx@ezweb.ne.jp |
| SoftBank| 090xxxxxxxx@softbank.ne.jp |
| Gmail（プッシュ通知） | あなたのアドレス@gmail.com |
