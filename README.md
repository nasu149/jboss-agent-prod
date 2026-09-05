# JBoss Incident Response Agent

LangGraph / Gemini / MCP / Human-in-the-loop を使った、**JBoss障害一次対応Agentの完成版デモ**です。

学習途中のサンプルや個別CLIは削除し、**Streamlitアプリを動かすために必要なものだけ**を残しています。
起動は基本的にこれだけです。

```bash
make app
```

このREADMEだけ読めば、セットアップ・操作方法・アーキテクチャ・各技術の役割・コードの読み方が分かるようにしています。

---

## 1. このアプリでできること

画面からFake JBossへ障害を注入し、Agentに障害対応をさせられます。

- Fake JBossの `server.log` を**前回cursor以降だけ**取得
- 新しいログがなければGeminiを呼ばず終了
- 新しいログがあればGeminiがIncidentか判定
- IncidentならTeamsへ通知（デフォルトはDry Run）
- Geminiが**read-only MCP Tool**を自分で選んで追加調査
- Geminiが原因と対処案をStructured Outputで返す
- PythonのRisk Policyが対処案を検証
- write操作の直前でLangGraph `interrupt()`
- Streamlit上で人間が **Approve / Reject / Edit & Approve**
- Approveされた場合だけMCP write Toolを実行
- Pythonで復旧状態を確認
- 復旧しなければ有限回だけ再調査
- SQLite Checkpointerにcursorと承認待ちStateを永続化
- UIでIncident、Tool利用、診断、復旧結果を確認

デモ用に以下のイベントを注入できます。

- `THREAD_POOL_CONFIGURATION`
- `DATASOURCE_POOL_EXHAUSTION`
- `DEPLOYMENT_FAILURE`
- `NORMAL_ACTIVITY`

Ground Truth（正解ラベル）はAgentから分離されたSQLiteに保存されるため、Agentは正解をカンニングできません。

---

## 2. 最短で動かす

### 前提

- Docker Desktop
- VS Code
- VS Code Dev Containers拡張
- Gemini API Key

### ① ZIPを展開してVS Codeで開く

展開したディレクトリそのものをVS Codeで開いてください。

### ② `.env` を作る

PowerShell:

```powershell
Copy-Item .env.example .env
```

bash:

```bash
cp .env.example .env
```

最低限これだけ設定します。

```env
GOOGLE_API_KEY=あなたのGemini API Key
```

Teamsを実際に送信しない場合は、そのまま以下でOKです。

```env
TEAMS_DRY_RUN=true
```

### ③ Dev Containerを開く

VS Codeで:

```text
Ctrl+Shift+P
→ Dev Containers: Reopen in Container
```

初回はDocker imageのbuildとPython packageのinstallが実行されます。

### ④ テスト

```bash
make test
```

### ⑤ アプリ起動

```bash
make app
```

ブラウザで:

```text
http://localhost:8501
```

`.streamlit/config.toml` にportとaddressを設定しているため、`make app` に長い引数は不要です。

---

## 3. 画面でのおすすめ操作

最初はこの順番で触るのが一番分かりやすいです。

### 1. `Inject Random Event`

Fake JBossにランダムな障害または正常イベントを発生させます。

### 2. `Run scan now`

Monitoring Graphが動きます。

```text
server.log
   ↓ cursor差分取得
新規ログあり？
   ├─ No  → 終了（Geminiを呼ばない）
   └─ Yes → Geminiで分類
                ↓
             Incident?
             ├─ No  → 終了
             └─ Yes → Teams通知
                         ↓
                    Incident Graph
```

### 3. Agentが自律調査

Incident GraphではGeminiにread-only MCP Toolだけを渡しています。

例:

```text
Gemini
  ↓
get_thread_pool_status を呼びたい
  ↓
MCP Tool
  ↓
ToolMessage
  ↓
Gemini
  ↓
追加調査 or 診断へ
```

### 4. Human approval

変更が必要な場合、Graphはwrite操作の直前で停止します。

```text
Risk Policy
   ↓
interrupt()
   ↓
PENDING_APPROVAL
```

Streamlitに承認UIが表示されます。

- Approve
- Reject
- Edit & Approve

### 5. Approveすると復旧処理

```text
Human Approve
   ↓
Command(resume=...)
   ↓
同じ thread_id のGraphを再開
   ↓
Pythonがwrite Toolを決定
   ↓
MCP write Tool
   ↓
Fake JBoss
   ↓
Recovery Verification
```

---

## 4. 全体アーキテクチャ

```text
┌──────────────────────────────────────────────────────────┐
│                    Streamlit app.py                      │
│  Inject / Scan / Approve / Incident / Activity          │
└──────────────────────────┬───────────────────────────────┘
                           │
                           ▼
                 ┌──────────────────┐
                 │   AgentService   │
                 └───────┬──────────┘
                         │
          ┌──────────────┴───────────────┐
          ▼                              ▼
┌───────────────────┐          ┌────────────────────┐
│ Monitoring Graph  │          │   Incident Graph   │
│                   │          │                    │
│ log delta         │          │ Gemini             │
│ Gemini classify   │          │   ↓                │
│ Teams notify      │          │ read MCP Tool loop │
└─────────┬─────────┘          │   ↓                │
          │                    │ diagnosis          │
          │                    │   ↓                │
          │                    │ Python Risk Policy │
          │                    │   ↓                │
          │                    │ interrupt()        │
          │                    │   ↓                │
          │                    │ Human approval     │
          │                    │   ↓                │
          │                    │ write MCP Tool     │
          │                    │   ↓                │
          │                    │ recovery verify    │
          │                    └─────────┬──────────┘
          │                              │
          └──────────────┬───────────────┘
                         ▼
              ┌─────────────────────┐
              │ LangChain MCP Tool  │
              └──────────┬──────────┘
                         │ stdio
                         ▼
              ┌─────────────────────┐
              │ Fake JBoss MCP      │
              │ Server              │
              └──────────┬──────────┘
                         ▼
              ┌─────────────────────┐
              │ File-backed         │
              │ Fake JBoss          │
              └─────────────────────┘
```

---

## 5. 誰が何を決めているのか

このプロジェクトで一番重要な設計ポイントです。

| 担当 | 任せていること |
|---|---|
| **Gemini** | ログの意味判断、次に調べるread Toolの選択、原因診断、対処案の提案 |
| **LangGraph** | State、Node/Edge、Tool loop、Conditional Edge、interrupt、再開、復旧loop |
| **Python** | Risk Policy、値の範囲チェック、write Toolの決定、復旧条件、ループ上限 |
| **MCP** | JBoss能力を明示的なToolとして別プロセスから公開 |
| **Human** | write操作の最終承認・拒否・値編集 |
| **SQLite Checkpointer** | LangGraph State、cursor、pending interrupt、thread_idの継続 |
| **Runtime SQLite** | Streamlit表示用のIncident概要・Activity |
| **Simulator SQLite** | Agentから見えないGround Truth |

### 特に重要な安全境界

Geminiには次のread Toolだけを渡します。

```text
read_server_log
get_server_health
get_thread_pool_status
get_datasource_status
get_deployment_status
get_recent_config_changes
```

Geminiにはwrite Toolをbindしません。

write Toolは:

```text
set_thread_pool_max_threads
set_datasource_max_pool_size
restart_deployment
reload_server
```

ですが、実行経路は必ず:

```text
Geminiの対処案
   ↓
Python Risk Policy
   ↓
Human Approval
   ↓
PythonがTool名を決定
   ↓
MCP write Tool
```

です。

`execute_shell` や `execute_jboss_cli` のような「何でもできるTool」は公開していません。

---

## 6. LangGraphは2つだけ

完成版ではGraphを2本に絞っています。

### Monitoring Graph

ファイル:

```text
src/jboss_agent/graphs/monitoring.py
```

役割:

```text
START
  ↓
start_cycle
  ↓
collect_logs
  ↓
新規ログあり？
 ├─ No → commit_cursor → END
 └─ Yes
      ↓
   analyze_logs (Gemini Structured Output)
      ↓
   Incident?
   ├─ No → commit_cursor → END
   └─ Yes
        ↓
     create_incident
        ↓
     notify_teams
        ↓
     commit_cursor
        ↓
       END
```

### Incident Graph

ファイル:

```text
src/jboss_agent/graphs/incident.py
```

役割:

```text
START
 ↓
prepare_investigation
 ↓
investigate (Gemini)
 ↓
read MCP Tool が必要？
 ├─ Yes → ToolNode → evidence → investigateへ
 └─ No  → diagnose
                 ↓
            validate_action
                 ↓
             Risk Policy
          ┌──────┼─────────┐
          │      │         │
       BLOCK   NONE    write候補
          │      │         ↓
         END    END    interrupt()
                            ↓
                          Human
                            ↓
                    prepare_write
                            ↓
                       ToolNode
                            ↓
                    verify_recovery
                     ├─ OK → END
                     └─ NG → 再調査
```

---

## 7. State / thread_id / Checkpointer

### Monitoring

固定thread_idを使います。

```text
monitor:jboss-01
```

そのため前回の:

```text
previous_log_cursor = 1234
```

をSQLite Checkpointerから引き継げます。

### Incident

Incidentごとに別threadです。

```text
incident:inc-xxxxxxxxxx
```

`interrupt()` が発生するとStateがSQLiteに保存されます。

Approve時には:

```python
Command(resume={"decision": "approve"})
```

を**同じthread_id**へ渡して再開します。

重要なのは、Python threadを起動しっぱなしにしているわけではないことです。
StateをSQLiteから復元して処理を再開しています。

---

## 8. MCPを使っている理由

Fake JBossはLangGraphプロセスの中のPython関数として直接呼ぶのではなく、別プロセスのMCP Serverとして公開します。

```text
LangGraph process
     │
     │ MCP / stdio
     ▼
Fake JBoss MCP Server process
```

これによりAgent側から見ると、JBossの実装詳細ではなく「利用可能なCapability」だけが見えます。

`src/jboss_agent/mcp/server.py` がMCP Server、`src/jboss_agent/mcp/client.py` がLangGraph側のadapterです。

Fake JBoss本体の状態はファイルに保存されるため、LangGraph側プロセスとMCP Server側プロセスの両方から同じ状態を参照できます。

---

## 9. TeamsはなぜMCPではないのか

Teams通知はアプリケーション自身が持つintegrationとして、ローカルのLangChain `@tool` にしています。

```text
src/jboss_agent/teams.py
```

つまりこのサンプルでは:

```text
JBossのCapability → MCP
Teams通知         → Local Tool
```

という違いを残しています。

デフォルト:

```env
TEAMS_DRY_RUN=true
```

なので実際にはTeamsへ送信せず、payloadだけ生成します。

実送信する場合:

```env
TEAMS_DRY_RUN=false
TEAMS_WEBHOOK_URL=https://...
```

---

## 10. データはどこに保存されるか

すべて `.data/` 配下です。

```text
.data/
├── checkpoints.sqlite   # LangGraph State / interrupt / cursor
├── runtime.sqlite       # UI表示用Incident / Activity
├── simulator.sqlite     # Ground Truth（Agentから分離）
└── fake_jboss/
    ├── state.json       # Fake JBossの状態
    └── server.log       # Fake server.log
```

`.data/` はGitにもZIPにも含めません。

デモを完全に初期化したい場合は、アプリを停止して:

```bash
make reset
```

その後もう一度:

```bash
make app
```

を実行してください。

---

## 11. ディレクトリ構成

意図的にかなり小さくしています。

```text
.
├── app.py                         # Streamlit UI / entry point
├── README.md
├── Makefile
├── Dockerfile
├── pyproject.toml
├── .env.example
├── .devcontainer/
│   └── devcontainer.json
├── .streamlit/
│   └── config.toml
├── src/jboss_agent/
│   ├── config.py                  # .env
│   ├── models.py                  # Structured Output schemas
│   ├── llm.py                     # Gemini client
│   ├── policy.py                  # deterministic Risk Policy
│   ├── service.py                 # UIとGraphを接続
│   ├── persistence.py             # SQLite Checkpointer
│   ├── runtime_store.py           # UI metadata SQLite
│   ├── simulator.py               # Fault injection + Ground Truth
│   ├── fake_jboss.py              # Fake JBoss本体
│   ├── teams.py                   # Local Teams Tool
│   ├── graphs/
│   │   ├── state.py
│   │   ├── prompts.py
│   │   ├── monitoring.py
│   │   └── incident.py
│   └── mcp/
│       ├── client.py
│       └── server.py
└── tests/
```

学習途中のサンプル、個別CLI、Scheduler、Evaluation Runner、重複Graphはありません。

---

## 12. コードを読むおすすめ順

### ① `app.py`

ユーザー操作が何を呼んでいるかを見る。

### ② `service.py`

Monitoring GraphとIncident Graphがどう繋がるかを見る。

### ③ `graphs/monitoring.py`

State / Node / Edge / Conditional Edge / cursorを見る。

### ④ `graphs/incident.py`

ToolNode / Agentic Tool loop / interrupt / Command(resume) / recovery loopを見る。

### ⑤ `mcp/client.py` と `mcp/server.py`

LangGraphと別プロセスのCapability境界を見る。

### ⑥ `policy.py`

LLMではなく普通のPythonに任せるべき制御を見る。

この順番だと全体像から詳細へ降りていけます。

---

## 13. Makefile

コマンドは必要最低限です。

```bash
make app      # Streamlit起動
make test     # pytest
make lint     # ruff
make check    # lint + test
make install  # Python依存を再install
make reset    # .dataを削除してデモ初期化
```

普段使うのはほぼ:

```bash
make app
```

だけです。

---

## 14. `.env` 一覧

| 変数 | 必須 | 用途 |
|---|---:|---|
| `GOOGLE_API_KEY` | Yes | Gemini API |
| `GEMINI_MODEL` | No | Gemini model名 |
| `GEMINI_TEMPERATURE` | No | temperature |
| `TEAMS_DRY_RUN` | No | Teams実送信を止める |
| `TEAMS_WEBHOOK_URL` | Teams実送信時 | Teams webhook |
| `SERVER_ID` | No | Fake JBoss ID |
| `FAKE_JBOSS_DATA_DIR` | No | Fake JBoss保存先 |
| `CHECKPOINT_DB_PATH` | No | LangGraph SQLite |
| `RUNTIME_DB_PATH` | No | UI metadata SQLite |
| `SIMULATOR_DB_PATH` | No | Ground Truth SQLite |
| `MAX_INVESTIGATION_ROUNDS` | No | read Tool調査の上限 |
| `MAX_RECOVERY_ATTEMPTS` | No | 復旧再試行の上限 |

---

## 15. テストで確認していること

`make test` では、少なくとも以下を確認します。

- `.env` 設定読込
- Risk Policyの許可 / BLOCK
- Fake JBoss writeのvalidationとidempotency
- Ground TruthがAgent-visible stateに混ざらないこと
- Runtime SQLiteのcursor / pending approval
- `langgraph-checkpoint-sqlite` が実際にinstallされていること
- Monitoring Graphが同一thread_idでcursorを引き継ぐこと
- 新規ログがない場合Geminiを呼ばないこと
- Incident Graphがinterruptで停止すること
- 同じthread_idに `Command(resume=...)` して再開できること
- Approve後だけwrite Toolが実行され、復旧判定まで進むこと

---

## 16. よくあるエラー

### `No module named 'langgraph.checkpoint.sqlite'`

SQLite CheckpointerはLangGraph本体とは別packageです。
この完成版では通常依存に明示的に入れています。

```toml
langgraph-checkpoint-sqlite>=3.1,<4
```

古いDev Containerを使い回している場合:

```bash
make install
```

またはVS Codeで:

```text
Dev Containers: Rebuild Container
```

を実行してください。

確認:

```bash
python -c "from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver; print('OK')"
```

### `GOOGLE_API_KEY is not configured`

`.env` を確認してください。

```env
GOOGLE_API_KEY=...
```

### Port 8501が開かない

Dev Containerのport forwardingを確認してください。

```text
Ports → 8501
```

### Reject後もFake JBossが障害状態

Rejectは「変更しない」が正しい挙動なので、その時点では障害は残ります。
次のFault Injection時にはサーバー状態だけ正常なbaselineへ戻してから新しいシナリオを入れるため、前の障害状態は混ざりません。
完全に履歴も含めて初期化したい場合はアプリを停止して:

```bash
make reset
```

---

## 17. このサンプルを本物のJBossへ発展させるなら

現在のFake JBossを置き換える境界はMCP Serverです。

つまりLangGraph側を大きく変えずに:

```text
Fake JBoss MCP Server
        ↓
Real JBoss Management API / JBoss CLI wrapper
```

へ変更できます。

ただし本番化では追加で以下が必要です。

- 認証・認可
- Secret管理
- TLS / network security
- Tool単位のRBAC
- write操作の監査ログ
- 冪等性key
- timeout / retry / circuit breaker
- 複数server対応
- PostgreSQL等の本番向けCheckpointer
- Teams以外の通知経路
- observability / tracing

このプロジェクトは、それらを載せる前の**Agent設計の最小骨格**として使う想定です。


## Troubleshooting

### `Gemini skipped` が続く場合

**Inject Random Event** のあとに **Run scan now** を押してください。v1.0.1 では MCP content block の正規化を共通化し、注入後の新規ログを正しく検出できるよう修正しています。
