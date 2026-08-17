"""Sleep and cardio tracking — the client-entered data the coach reviews."""


async def test_sleep_is_one_entry_per_night(client, auth_headers):
    first = await client.put(
        "/api/v1/wellness/sleep", headers=auth_headers, json={"hours_slept": 7.0, "quality": 3}
    )
    second = await client.put(
        "/api/v1/wellness/sleep", headers=auth_headers, json={"hours_slept": 8.5, "quality": 5}
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]  # corrected, not duplicated
    assert second.json()["hours_slept"] == 8.5


async def test_future_sleep_entries_are_refused(client, auth_headers):
    response = await client.put(
        "/api/v1/wellness/sleep",
        headers=auth_headers,
        json={"hours_slept": 8, "log_date": "2099-01-01"},
    )
    assert response.status_code == 400


async def test_multiple_cardio_sessions_per_day_are_allowed(client, auth_headers):
    for activity in ("walking", "cycling"):
        response = await client.post(
            "/api/v1/wellness/cardio",
            headers=auth_headers,
            json={"activity_type": activity, "duration_minutes": 30},
        )
        assert response.status_code == 201

    summary = await client.get("/api/v1/wellness/summary?days=1", headers=auth_headers)
    assert summary.json()["cardio_sessions"] >= 2
    assert summary.json()["cardio_minutes"] >= 60
