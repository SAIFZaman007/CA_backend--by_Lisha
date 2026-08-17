"""Account security behaviour."""


async def test_weak_password_is_rejected_with_a_usable_message(client):
    response = await client.post(
        "/api/v1/auth/register",
        json={"full_name": "Weak", "email": "weak@autonomyfitness.press", "password": "short"},
    )
    assert response.status_code == 422
    assert "password" in response.json()["fields"]


async def test_forgot_password_does_not_reveal_whether_an_account_exists(client):
    known = await client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "pytest.client@autonomyfitness.press"},
    )
    unknown = await client.post(
        "/api/v1/auth/forgot-password", json={"email": "nobody@autonomyfitness.press"}
    )
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()


async def test_protected_route_requires_a_token(client):
    assert (await client.get("/api/v1/dashboard")).status_code == 401


async def test_client_cannot_reach_coach_only_routes(client, auth_headers):
    response = await client.post(
        "/api/v1/exercises",
        headers=auth_headers,
        json={"name": "Hack Squat", "target_muscle": "Quads"},
    )
    assert response.status_code == 403
