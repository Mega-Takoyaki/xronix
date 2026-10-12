# ADR-0007 LambdaをVPCに入れない

状態: 採用　日付: 2026-10-12

## 決定

すべてのLambdaをVPCの外に置く。

## 理由

VPC内のLambdaが外部のX APIに出るには、NAT Gatewayが要り、月約$32の固定費がかかる。使うAWSサービス（DynamoDB、SSM等）はVPC外から利用でき、VPCの要件が無い。

## 結果

- ネットワーク制限はIAMとSSM／Cognitoの認証で担保する。
- VPCを必要とするリソース（RDS等）は採用しない。
