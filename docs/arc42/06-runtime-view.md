# 実行時ビュー

## 取得から通知まで

```mermaid
sequenceDiagram
    participant S as Scheduler
    participant F as 取得Lambda
    participant T as トークン管理
    participant X as X API
    participant D as DynamoDB
    participant C as 分類・返信案
    participant N as 通知

    S->>F: 定期起動
    F->>T: アクセストークン取得
    T-->>F: トークン（期限切れなら更新）
    F->>D: since_id 読み出し
    F->>X: GET mentions（since_id）
    X-->>F: 新着
    F->>D: 保存と since_id 更新
    D->>C: 新着あり
    C->>D: 分類と返信案を保存
    C->>N: 承認待ちを通知
```

- 読み取りは429／5xxで指数バックオフする。503は時々出る。
- 初回起動は過去分を遡るか、現在地点から始めるかを決める（TODO）。

## 承認から送信まで

```mermaid
sequenceDiagram
    participant O as 運用者
    participant A as 承認画面／API
    participant D as DynamoDB
    participant S as 送信Lambda
    participant X as X API

    O->>A: 承認（文面は編集可）
    A->>S: 送信依頼（同期呼び出し）
    S->>D: 停止・上限・ブロック・リンクを検査
    S->>D: 状態を approved→sending に条件付き更新
    S->>X: POST /2/tweets（1回のみ）
    alt 成功
        S->>D: sent と日次カウンタ加算
    else 失敗または結果不明
        S->>D: failed または unknown
        S-->>A: エラー表示（再送は人が判断）
    end
```

## トークン更新

- 期限が近い場合のみ更新する。
- DynamoDBでリースを取れた呼び出しだけがリフレッシュを実行する。
- 取れなかった呼び出しは短く待ち、更新後のトークンを読み直す。
- 新しい一対を保存してからリースを解放する。保存に失敗した場合は、再認可が必要になりうる（運用手順に記載する）。
