"""IndexNow key file: served only for the configured key."""

from app.core.config import settings


async def test_key_file_served_only_for_the_configured_key(client, monkeypatch):
    monkeypatch.setattr(settings, "INDEXNOW_KEY", "0123456789abcdef0123456789abcdef")

    ok = await client.get("/api/v1/meta/indexnow/0123456789abcdef0123456789abcdef.txt")
    assert ok.status_code == 200
    assert ok.text == "0123456789abcdef0123456789abcdef"

    wrong = await client.get("/api/v1/meta/indexnow/ffffffffffffffffffffffffffffffff.txt")
    assert wrong.status_code == 404


async def test_key_file_is_404_when_indexnow_is_off(client, monkeypatch):
    monkeypatch.setattr(settings, "INDEXNOW_KEY", "")
    response = await client.get("/api/v1/meta/indexnow/anything12.txt")
    assert response.status_code == 404