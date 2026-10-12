# デプロイビュー

| 項目 | 内容 |
| --- | --- |
| リージョン | ap-northeast-1 |
| 環境 | 検証環境（スタック `xronix-staging`）。mainへのpushで自動デプロイ（GitHub Actions＋OIDC）。開発はローカルのモックでも行える |
| ネットワーク | VPCなし。LambdaからXへは直接インターネットへ出る |
| 配信 | CloudFront標準ドメイン＋S3。独自ドメインは不要（必要になったら追加） |
| API | API Gateway HTTP API。認証はLambda内のBasic認証（ADR-0010）。画面はCloudFront FunctionでBasic認証 |
| データ | DynamoDB オンデマンド |
| 秘密情報 | SSM Parameter Store SecureString |
| 初回のX認可 | 画面の「Xと接続」から行う。コールバックはAPIの `/api/x/callback`（ADR-0009） |
| 定期起動 | 実行のたびに、次回を秒単位の一回限りのEventBridge Schedulerとして登録する（ADR-0013）。15分ごとの見守りが、遅れた予定を救済する |
| AI | Amazon Bedrock（LambdaのIAMロールで認証。東京リージョンが既定、画面で変更可） |
| 通知 | Telegram Bot API |

## 費用の見込み（月額、概算。推測を含む）

| 項目 | 見込み | 根拠 |
| --- | --- | --- |
| X API 読み取り | 約$0.1〜0.3 | メンション月50〜100件程度×$0.001。ユーザー展開の有無で増減 |
| X API 投稿作成 | 約$0.5〜1.5 | 月30〜100回×$0.015。返信の料金区分は未確認 |
| Lambda、DynamoDB、API Gateway、Scheduler | ほぼ$0 | 無料枠または極小 |
| CloudFront、S3 | ほぼ$0 | 無料枠内 |
| Cognito | $0 | 1人。無料枠内 |
| SSM SecureString（標準） | $0 | 標準パラメータは無料 |
| Secrets Manager を使う場合 | 約$0.4／シークレット | ADR-0003で比較 |
| CloudWatch Logs | 約$0〜0.5 | 保持期間を短く設定する |

合計は月$1〜3程度の見込み。デプロイ前に、実際の構成で再度見積もり、運用者の確認を取る。
