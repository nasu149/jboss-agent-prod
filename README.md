# JBoss Incident Agent - Learning Minimum

**LangGraph / Gemini / MCP / Human-in-the-loop を、コードを追いながら学ぶための最小デモ**です。

このリポジトリは「実運用に耐える障害対応基盤」を目指しません。
1 人が 1 回、Fake JBoss の疑似障害を解析して、必要なら人の承認後に復旧操作を行えれば十分、という前提で意図的に単純化しています。

## まず何を学ぶサンプルなのか

このデモで確認するポイントは 4 つだけです。

1. **State**: Node 間で値がどう受け渡されるか
2. **Conditional Edge**: Gemini の分類結果によって次の Node がどう変わるか
3. **MCP**: LangGraph プロセスから別プロセスの Fake JBoss Tool をどう呼ぶか
4. **Human-in-the-loop**: `interrupt()` で止まり、同じ `thread_id` を `Command(resume=...)` で再開するとはどういうことか

本番運用向けの機能は、学習の邪魔になるため削除しています。

---

## 1. 最短で動かす

前提:

- Docker Desktop
- VS Code
- Dev Containers 拡張
- Gemini API Key

`.env` を作ります。

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

API Key を設定します。

```env
GOOGLE_API_KEY=あなたのGemini API Key
```

VS Code で:

```text
Dev Containers: Reopen in Container
```

テスト:

```bash
make test
```

アプリ起動:

```bash
make app
```

ブラウザ:

```text
http://localhost:8501
```

---

## 2. 画面でやること

操作はほぼ 3 ステップです。

1. シナリオを選んで **「このシナリオを投入」**
2. **「Agent を実行」**
3. 変更が必要なら **Approve / Reject**

シナリオは従来と同じ 4 種類です。

- `THREAD_POOL_CONFIGURATION`
- `DATASOURCE_POOL_EXHAUSTION`
- `DEPLOYMENT_FAILURE`
- `NORMAL_ACTIVITY`

Fake JBoss にはシナリオ名そのものを保存しません。
Agent は `server.log` と MCP の read Tool の結果だけを見ます。

---

## 3. Graph 全体

Graph は **1 本だけ**です。

```text
START
  ↓
read_log                         ← MCP
  ↓
classify_log                     ← Gemini
  ↓
Conditional Edge(category)
  ├─ THREAD_POOL_CONFIGURATION
  │      ↓
  │  inspect_thread_pool         ← MCP read
  │
  ├─ DATASOURCE_POOL_EXHAUSTION
  │      ↓
  │  inspect_datasource          ← MCP read
  │
  ├─ DEPLOYMENT_FAILURE
  │      ↓
  │  inspect_deployment          ← MCP read
  │
  └─ NORMAL_ACTIVITY
         ↓
     normal_activity
         ↓
        END

障害3系統はここで合流
  ↓
approval                         ← interrupt()
  ↓
Approve / Reject                 ← Human
  ├─ Reject → rejected → END
  └─ Approve
        ↓
    execute_fix                  ← MCP write
        ↓
    verify_recovery              ← MCP read
        ↓
       END
```

### 一番見てほしい箇所

`src/jboss_agent/graph.py` のこの組み合わせです。

```python
graph.add_conditional_edges(
    "classify_log",
    route_category,
    {
        "THREAD_POOL_CONFIGURATION": "inspect_thread_pool",
        "DATASOURCE_POOL_EXHAUSTION": "inspect_datasource",
        "DEPLOYMENT_FAILURE": "inspect_deployment",
        "NORMAL_ACTIVITY": "normal_activity",
    },
)
```

Gemini が `category` を返す → State に入る → `route_category()` が読む → 次の Node が決まる、という流れです。

---

## 4. State は何を持つか

`AgentState` は必要最小限です。

| Key | 意味 |
|---|---|
| `server_id` | 対象 Fake JBoss |
| `log_lines` | MCP で取得した `server.log` |
| `category` | Gemini の分類結果 |
| `summary` | Gemini の分類理由 |
| `evidence` | 分岐先 MCP read Tool の結果 |
| `proposed_action` | Python が作った固定対処案 |
| `approved` | Human の判断 |
| `execution_result` | MCP write Tool の結果 |
| `recovered` | 復旧確認結果 |
| `status` | 画面表示用の最終状態 |
| `trace` | 通過した Node の順番 |

画面の `Node trace` を見ると例えば:

```text
read_log → classify_log → inspect_thread_pool → approval → execute_fix → verify_recovery
```

と表示されます。

---

## 5. LLM に何を任せているか

Gemini に任せているのは **ログ分類だけ**です。

```text
server.log
   ↓
Gemini
   ↓
THREAD_POOL_CONFIGURATION
or DATASOURCE_POOL_EXHAUSTION
or DEPLOYMENT_FAILURE
or NORMAL_ACTIVITY
```

対処 Tool 名や write 引数は Gemini に自由生成させません。

たとえば thread pool なら、分岐先 Node が固定で:

```text
set_thread_pool_max_threads(value=80)
```

を対処候補として作ります。

これは「LLM に任せる判断」と「Python で固定する制御」を分けて見やすくするためです。

---

## 6. MCP はどこで使うか

MCP Server は別 Python プロセスです。

```text
Streamlit / LangGraph process
         │
         │ stdio MCP
         ▼
jboss_agent.mcp.server
         │
         ▼
FakeJBoss
         │
         ├─ state.json
         └─ server.log
```

### read Tool

- `read_server_log`
- `get_thread_pool_status`
- `get_datasource_status`
- `get_deployment_status`

### write Tool

- `set_thread_pool_max_threads`
- `set_datasource_max_pool_size`
- `restart_deployment`

read/write を分けていますが、複雑な RBAC や Risk Policy はありません。
このデモでは **Human approval の後にしか write Node へ進まない**ことで境界を見せます。

---

## 7. Human-in-the-loop と Checkpoint

`approval` Node では:

```python
decision = interrupt({...})
```

を呼びます。

この瞬間に Graph は停止します。

今回は 1 人・1 回だけなので、SQLite や PostgreSQL は使わず:

```python
InMemorySaver()
```

を Checkpointer にしています。

ただし HITL の基本原理は同じです。

```text
初回 invoke
  ↓
interrupt()
  ↓
State を Checkpointer に保存
  ↓
画面で Approve
  ↓
Command(resume=True)
  ↓
同じ thread_id
  ↓
approval Node から再開
```

重要なのは、**Python thread をずっと待機させているわけではない**ことです。

このサンプルでは Streamlit の `session_state` に `InMemorySaver` を置くため、アプリプロセスを再起動すると Checkpoint は消えます。それで問題ない、という学習用の割り切りです。

---

## 8. Fake JBoss

実 JBoss を立てると学習対象が増えすぎるため、次だけを JSON / log で模擬します。

```text
.data/fake_jboss/
├── state.json
└── server.log
```

3 障害は非常に単純です。

### Thread Pool

```text
max_threads=20
active_threads=20
queue_size=37
```

承認後:

```text
max_threads=80
queue_size=0
```

### Datasource

```text
max_pool_size=5
timed_out_requests=14
```

承認後:

```text
max_pool_size=30
timed_out_requests=0
```

### Deployment

```text
app.war status=FAILED
```

承認後:

```text
app.war status=OK
```

現実の JBoss の挙動として正確であることより、LangGraph の処理を追いやすいことを優先しています。

---

## 9. ディレクトリ構成

```text
.
├── app.py
├── README.md
├── Dockerfile
├── Makefile
├── pyproject.toml
├── .env.example
├── .devcontainer/
├── .streamlit/
├── docs/
│   └── LEARNING_GUIDE.md
├── src/jboss_agent/
│   ├── config.py
│   ├── fake_jboss.py
│   ├── graph.py
│   └── mcp/
│       ├── client.py
│       └── server.py
└── tests/
    ├── test_fake_jboss.py
    ├── test_graph.py
    └── test_mcp.py
```

コードを読む順番は:

1. `graph.py`
2. `app.py`
3. `mcp/client.py`
4. `mcp/server.py`
5. `fake_jboss.py`

がおすすめです。

---

## 10. 今回あえて削ったもの

以前の構成には以下がありましたが、今回の学習目的には過剰なので削除しました。

- Monitoring Graph と Incident Graph の 2 Graph 構成
- ログ byte cursor と定期監視の状態管理
- Incident ID 管理
- Runtime SQLite
- Ground Truth SQLite
- SQLite Checkpointer
- Service 層
- Teams 通知
- Agentic read Tool loop
- LLM 診断用の別モデル呼び出し
- Risk Policy
- 値編集付き承認
- 復旧失敗時の再調査 loop
- 複数 Incident / 複数 user を想定した永続化

これらは「必要になった段階で 1 個ずつ追加する」方が LangGraph を理解しやすいです。

---

## 11. テスト

```bash
make check
```

確認内容:

- 3 障害が Fake JBoss 上で復旧できる
- LLM category ごとに期待した Node へ分岐する
- `interrupt()` 前には write が走らない
- `Command(resume=True)` で同じ Checkpoint を再開して write が走る
- Reject なら write が走らない
- `NORMAL_ACTIVITY` は HITL なしで終了する
- 実際に stdio MCP Server を起動し Tool を取得・実行できる

PR では GitHub Actions でも `ruff` と `pytest` を実行します。

---

## 12. 次に機能を足すなら

この最小版を理解したあとに、学習目的ごとに 1 個ずつ足すのがおすすめです。

```text
Step 1  ← 今ここ: 1 Graph / 1 run / InMemorySaver
Step 2  ToolNode を使って LLM 自身に read Tool を選ばせる
Step 3  SQLite Checkpointer に変える
Step 4  監視 cursor を追加する
Step 5  復旧失敗時の retry loop を追加する
Step 6  Teams の Local Tool を追加する
```

最初から全部入りに戻さないことが、このリポジトリを学習教材として使う上でのポイントです。
