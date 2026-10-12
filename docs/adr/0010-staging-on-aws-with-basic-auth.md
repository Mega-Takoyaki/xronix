# ADR-0010 検証環境をAWSに置き、認証は当面Basic認証にする

状態: 採用　日付: 2026-10-12

## 背景

ローカルのモックでは運用者が実際の画面で試せない。AWS上で、運用者がいつでも見られる検証環境を最優先にしたい。ログインは当面Basic認証で足りる、との判断を受けた。

## 決定

- 構成: S3＋CloudFront（画面）、同じドメインの `/api/*` をHTTP API→Lambdaへ。DynamoDB、SSM、EventBridge Scheduler（15分ごと）。
- 認証: Basic認証。画面はCloudFront Function、APIはLambda内で検証する。APIをLambda内で検証するのは、CloudFrontを迂回してAPI Gatewayの直接URLを叩かれても守るため。
- 資格情報は「ユーザー名:パスワード」のSHA-256のみを、GitHubのシークレットから生成して渡す。平文はAWSに置かない。
- 設定漏れで公開されないよう、ハッシュが未設定ならAPIは全拒否する。
- 例外: `/api/x/callback` は認証なし（stateとPKCEで保護）。
- 他サイトからの書き込みを防ぐため、書き込み系のAPIは専用ヘッダー `X-Requested-With: xronix` を要求する。
- 総当たりの抑制として、API Gatewayの受付を毎秒5件・瞬間10件に絞る。
- デプロイ: GitHub ActionsがOIDCで一時的な権限を引き受ける。Environment `staging` に限定し、必要なら承認者を置ける。mainへのpushでテスト、デプロイ、動作確認まで自動で行う。
- 失敗時の自動再実行は無効にする（定期処理のLambda）。

## 代替案と見送った理由

| 案 | 見送った理由 |
| --- | --- |
| Cognito | 実装済みだったが、当面はBasic認証で足りるとの判断により取り下げた。公開運用の段階で再検討する（ADR-0004） |
| 長期のアクセスキーをGitHubに保管 | 漏えい時の影響が大きい |
| CloudFrontだけでBasic認証 | API Gatewayの直接URLから迂回される |

## 結果

- Basic認証は試行回数の制限が無い。パスワードを20文字以上にすることが前提になる。
- ハッシュはCloudFront FunctionのコードとLambdaの環境変数に入る。AWSに入れる人には見える。
- AWSの固定費は発生しない見込み。費用は [手順書](../guide/aws-staging-setup.md) を参照。
