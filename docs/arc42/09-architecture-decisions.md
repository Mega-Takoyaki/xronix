# 設計判断

判断は [docs/adr/](../adr/) に1件ずつ記録する。

| ADR | 題 | 状態 |
| --- | --- | --- |
| [0001](../adr/0001-iac-sam.md) | IaCに AWS SAM を使う | 提案中 |
| [0002](../adr/0002-reply-generation.md) | 返信案はルールベースのテンプレートから始める | 提案中 |
| [0003](../adr/0003-token-storage-and-refresh.md) | トークンの保管と更新の排他制御 | 提案中 |
| [0004](../adr/0004-approval-ui-auth.md) | 承認画面の認証にCognitoを使う | 提案中 |
| [0005](../adr/0005-notification.md) | 通知手段 | 提案中（運用者の回答待ち） |
| [0006](../adr/0006-no-retry-on-create.md) | 投稿作成をリトライしない | 採用 |
| [0007](../adr/0007-no-vpc.md) | LambdaをVPCに入れない | 採用 |
| [0008](../adr/0008-dedicated-x-app.md) | 専用のXアプリを使う | 採用 |
| [0009](../adr/0009-ui-settings-and-job-control.md) | 設定は画面から行い、定期処理は短い間隔の起動＋設定上の間隔で制御する | 採用 |
