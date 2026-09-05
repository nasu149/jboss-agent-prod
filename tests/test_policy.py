from jboss_agent.policy import evaluate_action


def test_valid_thread_pool_change_requires_approval() -> None:
    result = evaluate_action(
        {"type": "SET_THREAD_POOL_MAX_THREADS", "current_value": 20, "proposed_value": 80}
    )
    assert result.allowed is True
    assert result.risk == "MEDIUM"


def test_out_of_range_write_is_blocked() -> None:
    result = evaluate_action(
        {"type": "SET_THREAD_POOL_MAX_THREADS", "current_value": 20, "proposed_value": 999}
    )
    assert result.allowed is False
    assert result.risk == "BLOCKED"


def test_generic_shell_action_is_blocked() -> None:
    assert evaluate_action({"type": "EXECUTE_SHELL"}).allowed is False
