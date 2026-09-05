def test_sqlite_checkpointer_dependency_is_installed() -> None:
    # Regression test for the missing dependency that previously broke the Streamlit app.
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    assert AsyncSqliteSaver is not None
