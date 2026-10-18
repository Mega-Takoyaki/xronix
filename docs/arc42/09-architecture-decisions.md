# 設計判断

判断は [docs/adr/](../adr/) に1件ずつ記録する。

| ADR | 題 | 状態 |
| --- | --- | --- |
| [0001](../adr/0001-iac-sam.md) | IaCに AWS SAM を使う | 提案中 |
| [0002](../adr/0002-reply-generation.md) | 返信案はルールベースのテンプレートから始める | 一部置き換え（ADR-0011） |
| [0003](../adr/0003-token-storage-and-refresh.md) | トークンの保管と更新の排他制御 | 提案中 |
| [0004](../adr/0004-approval-ui-auth.md) | 承認画面の認証にCognitoを使う | 採用（ADR-0017で導入） |
| [0005](../adr/0005-notification.md) | 通知手段 | 置き換え（ADR-0012） |
| [0006](../adr/0006-no-retry-on-create.md) | 投稿作成をリトライしない | 採用 |
| [0007](../adr/0007-no-vpc.md) | LambdaをVPCに入れない | 採用 |
| [0008](../adr/0008-dedicated-x-app.md) | 専用のXアプリを使う | 採用 |
| [0009](../adr/0009-ui-settings-and-job-control.md) | 設定は画面から行い、定期処理は短い間隔の起動＋設定上の間隔で制御する | 採用 |
| [0010](../adr/0010-staging-on-aws-with-basic-auth.md) | 検証環境をAWSに置く（認証はADR-0017で置き換え） | 採用 |
| [0011](../adr/0011-ai-bedrock-default.md) | AIの既定を Amazon Bedrock にし、OpenAI も選べるようにする | 採用 |
| [0012](../adr/0012-notify-telegram.md) | 通知は Telegram にする | 採用 |
| [0013](../adr/0013-precise-scheduling.md) | 次回実行を秒単位の一回限りのスケジュールで登録する | 採用 |
| [0014](../adr/0014-rules-and-actions.md) | 反応の種類ごとに、実行するアクションをルールで設定する | 採用 |
| [0015](../adr/0015-bedrock-model-list.md) | Bedrockのモデルを、使える一覧から選び、既定は安価なモデルを自動で選ぶ | 採用 |
| [0016](../adr/0016-wide-fetch.md) | 取得範囲を広げる（自分の@が無い返信、引用）。詳細ログ | 採用 |
| [0017](../adr/0017-cognito-login.md) | ログインをCognitoにし、Basic認証を廃止する | 採用 |
| [0018](../adr/0018-conversation-limits.md) | 同じ相手との会話に上限を置く | 採用 |
