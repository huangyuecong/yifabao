from uuid import uuid4


def create_task(client, headers, mode="full"):
    response = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "mode": mode,
            "name": f"{mode}-入口-{uuid4().hex[:8]}",
            "topic": "清溪河谷与臻品文化",
            "product": "臻品雅酿",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_library_file_versions_and_permissions(content_client, user_headers, other_headers):
    name = f"素材-{uuid4().hex[:8]}"
    uploaded = content_client.post(
        "/api/v1/library/items/upload",
        headers=user_headers,
        params={"category": "material", "name": name, "data_level": "internal"},
        files={"file": ("logo.png", b"mock-png", "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    item_id = uploaded.json()["id"]
    version = content_client.post(
        f"/api/v1/library/items/{item_id}/versions",
        headers=user_headers,
        json={
            "expected_version": uploaded.json()["version"],
            "category": "material",
            "name": name,
            "text_content": "第二版素材说明",
            "data_level": "internal",
        },
    )
    assert version.status_code == 200
    assert version.json()["version"] == 2
    assert (
        content_client.delete(
            f"/api/v1/library/items/{version.json()['id']}", headers=other_headers
        ).status_code
        == 403
    )

    denied_rule = content_client.post(
        "/api/v1/sensitive-rules",
        headers=other_headers,
        json={"phrase": f"禁词-{uuid4().hex}", "level": "forbidden"},
    )
    assert denied_rule.status_code == 403


def test_compliance_rule_types_are_reported_without_rewriting_content(
    content_client,
    user_headers,
):
    phrases = {
        "keyword": f"关键词-{uuid4().hex[:8]}",
        "sensitive": f"敏感词-{uuid4().hex[:8]}",
        "forbidden": f"禁用词-{uuid4().hex[:8]}",
    }
    for rule_type, phrase in phrases.items():
        created = content_client.post(
            "/api/v1/sensitive-rules",
            headers=user_headers,
            json={"phrase": phrase, "rule_type": rule_type},
        )
        assert created.status_code == 200, created.text
        assert created.json()["rule_type"] == rule_type

    task = create_task(content_client, user_headers, "copy")
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    copy_stage = next(item for item in workflow["stages"] if item["key"] == "copy")
    source_text = "，".join(phrases.values())
    imported = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/copy/import",
        headers=user_headers,
        json={"text": source_text, "expected_version": copy_stage["version"]},
    )
    assert imported.status_code == 200, imported.text
    checked = content_client.post(
        f"/api/v1/artifacts/{imported.json()['id']}/compliance-check",
        headers=user_headers,
    )
    assert checked.status_code == 200, checked.text
    result = checked.json()
    assert {hit["rule_type"] for hit in result["hits"]} == set(phrases)
    assert result["passed"] is False
    unchanged = content_client.get(
        f"/api/v1/tasks/{task['id']}/artifacts", headers=user_headers
    ).json()
    imported_after = next(item for item in unchanged if item["id"] == imported.json()["id"])
    assert imported_after["content"]["selected_candidate"]["body"] == source_text


def test_independent_modes_parameters_editing_and_egress(
    content_client, user_headers, other_headers
):
    tasks = [
        create_task(content_client, user_headers, mode)
        for mode in ("full", "copy", "image", "video")
    ]
    assert {task["mode"] for task in tasks} == {"full", "copy", "image", "video"}
    task = tasks[0]

    access = content_client.put(
        f"/api/v1/tasks/{task['id']}/access",
        headers=user_headers,
        json={"user_ids": ["user-b"], "departments": []},
    )
    assert access.status_code == 200
    assert (
        content_client.get(f"/api/v1/tasks/{task['id']}", headers=other_headers).status_code == 200
    )

    images = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-images",
        headers=user_headers,
        json={
            "count": 1,
            "parameters": {"scene": "酒库", "style": "纪实", "ratio": "9:16", "size": "1080x1920"},
        },
    )
    assert images.status_code == 200, images.text
    assert images.json()[0]["content"]["ratio"] == "9:16"
    assert "画面比例：9:16" in images.json()[0]["content"]["prompt"]

    voice = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-voice",
        headers=user_headers,
        json={
            "instruction": "臻品文化，源于长期坚持。",
            "parameters": {"voice": "沉稳女声", "speed": 1.2, "volume": 0.8},
        },
    )
    assert voice.status_code == 200, voice.text
    assert voice.json()["content"]["voice"] == "沉稳女声"
    assert voice.json()["content"]["speed"] == 1.2

    copy = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    ).json()
    revised = content_client.patch(
        f"/api/v1/artifacts/{copy['id']}",
        headers=user_headers,
        json={
            "content": {"body": "人工校订后的品牌文案"},
            "expected_version": copy["version"],
        },
    )
    assert revised.status_code == 200
    assert revised.json()["version"] > copy["version"]
    transformed = content_client.post(
        f"/api/v1/artifacts/{revised.json()['id']}/transform-copy",
        headers=user_headers,
        json={"operation": "shorten", "instruction": "缩写为一句话"},
    )
    assert transformed.status_code == 200
    assert transformed.json()["content"]["transformation"] == "shorten"

    provider_id = content_client.get(
        f"/api/v1/tasks/{task['id']}/egress-status",
        headers=user_headers,
    ).json()["provider_id"]
    denied = content_client.post(
        f"/api/v1/tasks/{task['id']}/egress-grants",
        headers=other_headers,
        json={"provider": provider_id, "allowed_data_levels": ["public"]},
    )
    assert denied.status_code == 403
    granted = content_client.post(
        f"/api/v1/tasks/{task['id']}/egress-grants",
        headers=user_headers,
        json={"provider": provider_id, "allowed_data_levels": ["public", "internal"]},
    )
    assert granted.status_code == 200

    audit = content_client.get("/api/v1/audit-logs", headers=user_headers)
    actions = {row["action"] for row in audit.json()}
    assert {"artifact.revised", "copy.transformed", "egress.approved"} <= actions
