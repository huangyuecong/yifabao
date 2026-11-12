"""Pagination and lightweight concurrency regression tests for content lists."""

from concurrent.futures import ThreadPoolExecutor
from statistics import quantiles
from time import perf_counter
from uuid import uuid4


def _create_task(client, headers, name):
    response = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"mode": "full", "name": name, "topic": "分页测试"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_task_pagination_filter_and_max_page_size(content_client, user_headers):
    marker = uuid4().hex[:8]
    for index in range(3):
        _create_task(content_client, user_headers, f"分页任务-{marker}-{index}")

    first = content_client.get(
        "/api/v1/tasks", params={"page": 1, "page_size": 2}, headers=user_headers
    )
    assert first.status_code == 200
    assert len(first.json()) <= 2
    filtered = content_client.get(
        "/api/v1/tasks",
        params={"keyword": marker, "page_size": 100},
        headers=user_headers,
    )
    assert filtered.status_code == 200
    assert all(marker in task["name"] for task in filtered.json())
    too_large = content_client.get("/api/v1/tasks", params={"page_size": 101}, headers=user_headers)
    assert too_large.status_code == 422


def test_task_pagination_filters_visibility_before_offset(
    content_client, user_headers, other_headers
):
    """A newer burst of another user's tasks must not hide this user's history."""

    marker = uuid4().hex[:8]
    own = _create_task(content_client, user_headers, f"历史保留-{marker}")
    for index in range(55):
        _create_task(content_client, other_headers, f"其他用户新任务-{marker}-{index}")

    response = content_client.get(
        "/api/v1/tasks",
        params={"page": 1, "page_size": 50},
        headers=user_headers,
    )
    assert response.status_code == 200, response.text
    assert own["id"] in {item["id"] for item in response.json()}


def test_library_pagination_and_keyword(content_client, user_headers):
    marker = uuid4().hex[:8]
    for index in range(3):
        response = content_client.post(
            "/api/v1/library/items",
            headers=user_headers,
            json={
                "category": "pagination",
                "name": f"资料-{marker}-{index}",
                "text_content": f"正文-{marker}",
            },
        )
        assert response.status_code == 200, response.text
    response = content_client.get(
        "/api/v1/library/items",
        params={"keyword": marker, "page": 1, "page_size": 2},
        headers=user_headers,
    )
    assert response.status_code == 200
    assert len(response.json()) <= 2


def test_one_hundred_concurrent_task_reads_have_no_errors(content_client, user_headers):
    # This is a smoke benchmark rather than a machine-specific latency gate.
    def read_once(_index):
        started = perf_counter()
        response = content_client.get(
            "/api/v1/tasks", params={"page": 1, "page_size": 20}, headers=user_headers
        )
        return response.status_code, perf_counter() - started

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(read_once, range(100)))
    durations = sorted(duration for status, duration in results)
    statuses = [status for status, _ in results]
    p50 = durations[49]
    p95 = quantiles(durations, n=20)[18]
    print(
        f"concurrent_reads count=100 p50={p50:.4f}s p95={p95:.4f}s error_rate={sum(status != 200 for status in statuses) / 100:.4f}"
    )
    assert all(status == 200 for status in statuses)
    assert p50 >= 0 and p95 >= p50
