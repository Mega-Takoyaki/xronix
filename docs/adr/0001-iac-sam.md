# ADR-0001 IaCに AWS SAM を使う

状態: 提案中　日付: 2026-10-12

## 背景

IaCは SAM か CDK（Python）で、提案して決めてよい。対象は小規模で、運用者は1人。Lambdaをローカルでテスト可能にしたい。

## 選択肢

| 観点 | SAM | CDK（Python） |
| --- | --- | --- |
| 記述 | YAML。リソースが見たまま分かる | Pythonで抽象化できる |
| 追加依存 | SAM CLI | CDK CLI（Node.js）＋ブートストラップ（S3等の事前リソース） |
| ローカル実行 | `sam local invoke` が標準 | `sam local` を併用可能だが手間が増える |
| Lambdaのビルド | `sam build` で完結 | 追加の構成が要る |
| CloudFront／Cognito等 | 素のCloudFormationで記述。やや冗長 | 高水準Constructで短い |
| Windows 11での導入 | SAM CLIのみ | Node.js も必要 |

## 決定（提案）

SAM を採用する。構成が小さく、YAMLの冗長さより「依存が少ない」「sam localで確認できる」利点が大きい。

## 結果

- CloudFront／Cognito／S3の記述は長くなる。許容する。
- 構成が膨らみ管理が苦しくなった場合は、CDKへの移行を再検討する。
