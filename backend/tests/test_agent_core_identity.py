from agent_core.identity import Identity, get_identity


def test_identity_role_checks_and_header_parsing():
    user = Identity("user-a", "用户A", "数字中心", frozenset({"user"}))
    assert user.has_role("user") is True
    assert user.has_role("admin") is False
    parsed = get_identity(
        x_user_id="user-b",
        x_user_name="用户B",
        x_department="市场部",
        x_roles="user, reviewer, ,admin",
    )
    assert parsed.user_id == "user-b"
    assert parsed.roles == frozenset({"user", "reviewer", "admin"})
