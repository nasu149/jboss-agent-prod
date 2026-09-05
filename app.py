"""JBoss 障害対応デモの画面表示とユーザー操作を担当する Streamlit アプリ。

サーバー状態の確認、疑似イベントの投入、監視の実行、復旧操作の承認、結果の
答え合わせを提供する。監視・復旧ワークフローの実行は AgentService に委譲する。
Streamlit は操作のたびにこのファイルを上から再実行するため、処理結果は
永続ストアから読み直し、画面セッション固有の情報は st.session_state に保持する。
"""

from __future__ import annotations

import asyncio
from typing import Any

import streamlit as st

from jboss_agent.config import get_settings
from jboss_agent.fake_jboss import FakeJBossOperations
from jboss_agent.runtime_store import IncidentRecord, RuntimeStore
from jboss_agent.service import AgentService
from jboss_agent.simulator import (
    SCENARIOS,
    FaultInjector,
    GroundTruthEvent,
    GroundTruthStore,
    normalize_diagnosis,
)

# 設定と永続ストアを用意する。runtime は監視・障害・活動履歴、truth は
# 答え合わせ用の正解データを扱い、正解はエージェントの診断入力に含めない。
settings = get_settings()
runtime = RuntimeStore(settings.runtime_db_path)
truth = GroundTruthStore(settings.simulator_db_path)
fake = FakeJBossOperations(settings.fake_jboss_data_dir, server_id=settings.server_id)
# 再実行時に既存の状態をリセットせず、未作成の状態ファイルとログだけを初期化する。
fake.ensure_initialized()
# 疑似イベントの投入と、監視・復旧ワークフローの実行をそれぞれ専用クラスに任せる。
injector = FaultInjector(fake, truth)
service = AgentService(settings, runtime, truth)


def run_async(coro: Any) -> Any:
    """同期処理の Streamlit から、操作ごとにイベントループを作って非同期処理を実行する。"""
    return asyncio.run(coro)


def run_agent_action(coro: Any, label: str) -> bool:
    """非同期のエージェント操作を実行し、進行中の表示と例外処理を共通化する。

    coro は実行するコルーチン、label はスピナーに表示する説明。
    例外なく終了した場合は True、例外を画面に表示した場合は False を返す。
    この戻り値は操作の実行成否を表し、障害が復旧したかどうかは表さない。"""
    try:
        with st.spinner(label):
            run_async(coro)
        return True
    except Exception as exc:  # 操作中のエラーは、画面上のメッセージとして表示する。
        st.error(f"エージェントの処理に失敗しました: {exc}")
        return False


def terminal(record: IncidentRecord) -> bool:
    """障害が調査中・承認待ちを抜け、答え合わせ可能な状態かを返す。

    復旧成功に限らず、拒否や安全ルールによる中止なども終了状態として扱う。"""
    return record.status not in {"INVESTIGATING", "PENDING_APPROVAL"}


def render_sidebar() -> None:
    """デモの操作手順と、モデル・対象サーバー・通知モードの設定を表示する。"""
    with st.sidebar:
        st.header("How to try it")
        st.markdown(
            "1. **Inject Random Event**\n"
            "2. **Run scan now**\n"
            "3. 対処案が表示されたら、**承認して実行 / 拒否する**\n"
            "4. Check the incident and activity tables"
        )
        st.divider()
        st.caption(f"Gemini: `{settings.gemini_model}`")
        st.caption(f"Server: `{settings.server_id}`")
        st.caption(f"Teams: `{'DRY RUN' if settings.teams_dry_run else 'LIVE'}`")
        st.caption("JBoss backend: Fake JBoss via MCP/stdio")


def render_server_snapshot() -> None:
    """Fake JBoss の現在の稼働状態、エラー率、リソース使用量を取得して表示する。"""
    health = fake.get_server_health(settings.server_id)
    thread_pool = fake.get_thread_pool_status(settings.server_id)
    datasource = fake.get_datasource_status(settings.server_id)
    deployment = fake.get_deployment_status(settings.server_id)

    st.subheader("Server status")
    cols = st.columns(4)
    cols[0].metric("Server", str(health["status"]))
    cols[1].metric("Error rate", f"{float(health['request_error_rate']):.1%}")
    cols[2].metric(
        "Thread pool",
        f"{thread_pool['active_threads']}/{thread_pool['max_threads']}",
        help=f"queue={thread_pool['queue_size']}, rejected={thread_pool['rejected_tasks']}",
    )
    cols[3].metric(
        "Datasource",
        f"{datasource['active_count']}/{datasource['max_pool_size']}",
        help=f"timeouts={datasource['timed_out_requests']}",
    )
    st.caption(
        f"Deployment {deployment['name']}: status={deployment['status']}, enabled={deployment['enabled']}"
    )


def render_monitoring_status() -> None:
    """永続ストアから監視状態、ログの読取位置、最終スキャン日時とエラーを表示する。"""
    status = runtime.get_monitoring_status(settings.server_id)
    st.subheader("Monitoring state")
    cols = st.columns(4)
    cols[0].metric("Status", status.status)
    cols[1].metric("Log cursor", status.current_cursor)
    cols[2].metric("Previous cursor", status.previous_cursor)
    cols[3].metric("Last scan", status.last_scan_at or "—")
    if status.last_error:
        st.error(status.last_error)


def render_controls() -> None:
    """監視を一度実行するボタンと、疑似イベントを投入するボタンを表示する。

    監視には API キーが必要。イベント投入時は正解の参照 ID をセッションに保存し、
    活動履歴には正解を含めずに投入の事実を記録する。"""
    # 承認対象のサーバー状態を別のイベントで上書きしないよう、承認待ち中は投入を無効にする。
    pending = runtime.list_pending_approvals()
    st.subheader("Controls")
    left, right = st.columns(2)

    if left.button(
        "Run scan now",
        type="primary",
        use_container_width=True,
        disabled=not settings.has_google_api_key,
    ):
        # 監視から必要に応じて障害調査へ進む。成功後は先に描画した状態表示も更新する。
        if run_agent_action(service.run_scan(), "Running Monitoring Graph..."):
            st.rerun()

    if right.button(
        "Inject Random Event",
        use_container_width=True,
        disabled=bool(pending),
        help="Injects a fake fault or normal event. The hidden answer is not passed to the Agent.",
    ):
        event = injector.inject_random()
        st.session_state["last_injected_event_id"] = event.event_id
        runtime.add_activity(
            settings.server_id,
            "simulator",
            "Random simulator event injected; Ground Truth hidden from Agent",
        )
        st.success("Event injected. Now click Run scan now.")

    with st.expander("Choose a specific demo scenario"):
        chosen = st.selectbox("Scenario", SCENARIOS)
        if st.button("Inject selected scenario", disabled=bool(pending)):
            event = injector.inject(chosen)
            st.session_state["last_injected_event_id"] = event.event_id
            runtime.add_activity(
                settings.server_id,
                "simulator",
                "Selected simulator event injected; Ground Truth hidden from Agent",
            )
            st.success("Scenario injected. Now click Run scan now.")


def render_approvals() -> None:
    """承認待ちの復旧案を表示し、ユーザーの判断で停止中のワークフローを再開する。

    操作内容・リスク・提案理由を示し、承認、拒否、整数の提案値を編集した承認を
    受け付ける。再開処理が例外なく終了したら、画面全体を最新の状態で描画し直す。"""
    pending = runtime.list_pending_approvals()
    st.subheader("復旧操作の承認")
    if not pending:
        st.info("承認待ちの復旧操作はありません。")
        return

    st.caption(
        "対処内容を確認してください。承認するとサーバーへの変更を実行します。拒否すると変更せずに終了します。"
    )
    for record in pending:
        payload = record.pending_approval or {}
        with st.container(border=True):
            st.markdown(f"**対象の障害: `{record.incident_id}`**")
            st.caption(f"対象サーバー: {record.server_id}")
            c1, c2, c3 = st.columns(3)
            # 内部の操作コードは維持し、人が判断する画面では日本語の名称を使う。
            action_labels = {
                "SET_THREAD_POOL_MAX_THREADS": "スレッドプールの最大スレッド数を変更",
                "SET_DATASOURCE_MAX_POOL_SIZE": "データソースの最大接続数を変更",
                "RESTART_DEPLOYMENT": "アプリケーションを再起動",
                "RELOAD_SERVER": "サーバーを再読み込み",
                "NONE": "変更なし",
            }
            risk_labels = {"LOW": "低", "MEDIUM": "中", "HIGH": "高", "BLOCKED": "実行不可"}
            c1.write(f"操作: {action_labels.get(payload.get('action'), '不明な操作')}")
            c2.write(f"リスク: **{risk_labels.get(payload.get('risk'), '不明')}**")
            if payload.get("proposed_value") is not None:
                current = payload.get("current_value")
                c3.write(
                    f"現在の値 → 変更後の値: `{current if current is not None else '不明'}` → `{payload['proposed_value']}`"
                )
            elif payload.get("deployment_name"):
                c3.write(f"対象アプリケーション: `{payload['deployment_name']}`")
            else:
                c3.write("対象: サーバー全体")
            st.write("提案理由: " + (payload.get("reason") or "理由は提示されていません。"))

            # 障害ごとに固有の key を付け、複数の承認欄があってもボタンを区別する。
            # 判断結果はサービス経由で、保存された承認待ちワークフローに渡す。
            approve, reject = st.columns(2)
            if approve.button(
                "承認して実行",
                key=f"approve-{record.incident_id}",
                use_container_width=True,
            ):
                if run_agent_action(
                    service.resume_incident(record.incident_id, decision="approve"),
                    "承認した復旧操作を実行しています...",
                ):
                    st.rerun()

            if reject.button(
                "拒否する",
                key=f"reject-{record.incident_id}",
                use_container_width=True,
            ):
                if run_agent_action(
                    service.resume_incident(record.incident_id, decision="reject"),
                    "復旧操作を拒否して終了しています...",
                ):
                    st.rerun()

            default_value = payload.get("proposed_value")
            # 編集された値もグラフ側で安全ルールを再検証してから実行する。
            if isinstance(default_value, int):
                edited = st.number_input(
                    "変更後の値を編集",
                    value=default_value,
                    step=1,
                    key=f"edit-value-{record.incident_id}",
                )
                if st.button(
                    "編集した値で承認・実行",
                    key=f"edit-approve-{record.incident_id}",
                    use_container_width=True,
                ):
                    if run_agent_action(
                        service.resume_incident(
                            record.incident_id,
                            decision="edit_and_approve",
                            proposed_value=int(edited),
                        ),
                        "編集した値の安全性を確認し、復旧操作を実行しています...",
                    ):
                        st.rerun()


def render_incidents() -> None:
    """直近 20 件の障害について、対応状況・診断分類・重要度・復旧結果などを一覧表示する。"""
    incidents = runtime.list_incidents(limit=20)
    st.subheader("Incidents")
    if not incidents:
        st.info("No incidents yet.")
        return

    st.dataframe(
        [
            {
                "incident_id": item.incident_id,
                "status": {
                    "INVESTIGATING": "調査中",
                    "PENDING_APPROVAL": "承認待ち",
                    "REJECTED": "拒否済み",
                    "BLOCKED": "安全ルールにより中止",
                    "RECOVERED": "復旧済み",
                    "RESOLVED_NO_ACTION": "変更なしで完了",
                    "FAILED_SAFE": "復旧失敗・担当者の対応待ち",
                    "COMPLETED": "完了",
                }.get(item.status, item.status),
                "category": item.category,
                "severity": item.severity,
                "confidence": item.confidence,
                "read_tool_calls": item.investigation_tool_calls,
                "recovered": item.recovered,
                "created_at": item.created_at,
            }
            for item in incidents
        ],
        use_container_width=True,
        hide_index=True,
    )


def ground_truth_is_revealable(event: GroundTruthEvent) -> bool:
    """疑似イベントの正解を画面に公開できるかを判定する。

    障害に紐づく場合は、その障害が終了状態になるまで待つ。
    紐づく障害がない場合は、最終スキャン日時がイベント投入日時以降かで判定する。"""
    if event.linked_incident_id:
        record = runtime.get_incident(event.linked_incident_id)
        return bool(record and terminal(record))
    monitoring = runtime.get_monitoring_status(event.server_id)
    return bool(monitoring.last_scan_at and monitoring.last_scan_at >= event.injected_at)


def render_ground_truth() -> None:
    """公開条件を満たした疑似イベントについて、診断と復旧結果の答え合わせを表示する。

    このセッションで最後に投入したイベントを優先し、参照 ID がなければ対象サーバーの
    最新イベントを使う。障害が作成されなかった場合は、正常イベントか検知漏れかを示す。"""
    st.subheader("Demo answer check")
    event_id = st.session_state.get("last_injected_event_id")
    event = truth.get(event_id) if event_id else truth.latest(settings.server_id)
    if event is None:
        st.info("Inject an event to create a hidden Ground Truth answer.")
        return
    if not ground_truth_is_revealable(event):
        st.warning("Ground Truth is hidden until the Agent finishes this event.")
        return

    st.write(f"Injected scenario: **{event.scenario}**")
    if event.linked_incident_id:
        record = runtime.get_incident(event.linked_incident_id)
        if record is None:
            return
        # 診断の表記揺れをシナリオ名に正規化してから、投入した正解と照合する。
        actual = normalize_diagnosis((record.diagnosis or {}).get("root_cause"))
        st.write(f"Agent diagnosis: **{actual or 'N/A'}**")
        st.write(f"Diagnosis: **{'Correct' if actual == event.scenario else 'Incorrect'}**")
        st.write(f"Recovery: **{'Success' if record.recovered else 'Failed / not applicable'}**")
    else:
        st.write("Agent created no incident.")
        st.write("Detection: **Correct**" if event.scenario == "NORMAL_ACTIVITY" else "Detection: **Missed**")


def render_activity() -> None:
    """対象サーバーの直近 80 件の活動履歴を、古いものから順に表示する。"""
    rows = runtime.list_activity(settings.server_id, limit=80)
    st.subheader("Agent activity timeline")
    if not rows:
        st.info("No activity yet.")
        return
    # ストアは新しい順に返すため、表示時は処理の流れを追えるよう古い順に並べる。
    rows.reverse()
    st.dataframe(
        [
            {
                "time": row["timestamp"],
                "incident": row["incident_id"] or "",
                "type": row["event_type"],
                "activity": row["message"],
            }
            for row in rows
        ],
        use_container_width=True,
        hide_index=True,
    )


# ここから画面の組み立て。Streamlit により、初回表示時とユーザー操作時に実行される。
st.set_page_config(page_title="JBoss Incident Agent", page_icon="🧭", layout="wide")
st.title("JBoss Incident Response Agent")
st.caption("LangGraph + Gemini + MCP + Human-in-the-loop, with a local Fake JBoss backend")

# キー未設定でも状態や履歴は表示し、API を使うスキャンボタンは render_controls で無効にする。
if not settings.has_google_api_key:
    st.error("GOOGLE_API_KEY is not configured. Copy `.env.example` to `.env` and set your Gemini API key.")

# 操作に必要な状態とコントロールを先に、承認・障害一覧・答え合わせ・活動履歴を後に配置する。
render_sidebar()
render_server_snapshot()
render_monitoring_status()
render_controls()
render_approvals()
render_incidents()
render_ground_truth()
render_activity()
