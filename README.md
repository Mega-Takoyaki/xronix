# XroniX（ズロニクス）

X（旧Twitter）アカウント @GirlAidol に付く返信・メンションを、見逃さず、無理のない手間で返すための個人用アプリ。AWSサーバレスで動かす。

## 現在の状態

MVPをローカルで動かせる（モックモード）。AWSの検証環境へ、mainへのpushで自動デプロイする仕組みを用意した（手順は [docs/guide/aws-staging-setup.md](docs/guide/aws-staging-setup.md)）。実際のデプロイは未実施。

| 機能 | 状態 |
| --- | --- |
| 定期処理（有効／無効、15分〜24時間の間隔、今すぐ実行、実行履歴） | ローカルで確認済み |
| メンション取得、分類、返信案、承認画面、送信 | ローカル（モック）で確認済み |
| 定型文の管理、ブロック対象、1日の上限、緊急停止 | ローカルで確認済み |
| ログ・統計（日別、分類別、概算費用） | ローカルで確認済み |
| X接続（OAuth2＋PKCE）、AI接続（Grok）、Discord通知 | 単体テスト（偽のHTTP）のみ。実機は未確認 |
| DynamoDB／SSMの実装、Lambda入口 | motoでテスト済み。実AWSは未確認 |
| SAMテンプレート（`template.yaml`）、事前準備（`infra/bootstrap.yaml`）、自動デプロイ（`.github/workflows/deploy.yml`） | lint・ビルド・Basic認証のロジックのテストまで。実AWSへのデプロイは未実施 |

## ローカルで動かす

Python 3.12以上が必要。

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # Windowsは .venv\Scripts\pip
.venv/bin/pytest -q
.venv/bin/python -m xronix.devserver     # http://127.0.0.1:8787/
```

- 既定はモックモード。Xへは何も送信されない。「設定」で「Xと接続」を押し、「ジョブ」の「サンプルのメンションを追加」→「今すぐ実行」で一巡できる。
- 初回の実行は「現在地点から始める」ため、過去分は取り込まれない。サンプルは2回目の実行で取り込まれる。
- データは `.xronix/`（SQLiteと秘密情報のファイル）に保存される。Gitには含まれない。
- 実機のXに接続する場合は `XRONIX_X_MODE=live` で起動し、画面でClient IDとSecretを設定する（手順は [docs/TODO.md](docs/TODO.md)）。この場合、承認した返信は実際に投稿される。

## 構成

```
src/xronix/   コア（api.py が全操作の入口。devserver.py と lambda_handlers.py が薄い入口）
web/          日本語の管理画面（静的HTML／JS）
template.yaml  SAMテンプレート（検証環境）
infra/        事前準備のテンプレート（OIDCとデプロイ用ロール）
tests/        pytest（69件）
docs/         arc42、ADR、TODO
```

## ドキュメント体系

arc42 をベースにし、判断は ADR、作業は TODO に分けて記録する。

| 場所 | 内容 |
| --- | --- |
| [docs/arc42/](docs/arc42/) | アーキテクチャ文書（arc42 の章立て） |
| [docs/adr/](docs/adr/) | 設計判断の記録（状態: 提案中 / 採用 / 却下） |
| [docs/TODO.md](docs/TODO.md) | 未決事項、確認事項、作業 |

## 方針の要約

- 半自動。新着取得、分類、返信案、人が承認、送信の順。自動送信は含めない。
- LambdaはVPCに入れない。
- 成人向けの文脈を含むため、Claude（Bedrock含む）には送らない。AIを使う場合はGrokのみで、既定はオフ。
- ReelMilly とは別アプリ。コードもXトークンも共有しない。
- 秘密情報は SSM / ローカルファイルのみに置き、コミットしない。画面にも表示しない。
