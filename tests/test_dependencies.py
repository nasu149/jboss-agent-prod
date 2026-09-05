def test_sqlite_checkpointer_dependency_is_installed() -> None:
    # 依存不足で Streamlit が起動できなくなる問題の再発を防ぐ。
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    assert AsyncSqliteSaver is not None
