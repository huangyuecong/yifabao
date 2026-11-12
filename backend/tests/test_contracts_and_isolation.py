def test_content_service_stays_independent(content_client, user_headers):
    health = content_client.get("/health", headers=user_headers)
    assert health.status_code == 200
    assert health.headers["X-Agent-Service"] == "content-service"
    assert content_client.get("/dev").status_code == 404

    paths = content_client.get("/openapi.json").json()["paths"]
    assert "/api/v1/tasks" in paths
    assert "/api/v1/meetings" not in paths
