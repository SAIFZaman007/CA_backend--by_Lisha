"""Storage key parsing and the local backend's path safety."""

from app.services import storage


def test_cloud_key_round_trip():
    key = storage.CloudKey("image", "authenticated", "coach-auto/progress/a/b/c").encode()
    parsed = storage.parse_key(key)
    assert parsed is not None
    assert parsed.resource_type == "image"
    assert parsed.delivery_type == "authenticated"
    assert parsed.public_id == "coach-auto/progress/a/b/c"


def test_local_keys_are_not_remote():
    assert storage.parse_key("gallery/abc.jpg") is None
    assert not storage.is_remote("gallery/abc.jpg")


def test_malformed_cloud_keys_are_rejected():
    assert storage.parse_key("cld:image") is None
    assert storage.parse_key("cld:pdf:upload:x") is None
    assert storage.parse_key("cld:image:private:x") is None


def test_local_paths_cannot_escape_the_upload_root():
    assert not storage.exists("../../etc/passwd")


def test_public_url_is_none_for_local_and_private_keys():
    assert storage.public_url("gallery/abc.jpg") is None
    assert storage.public_url("cld:image:authenticated:coach-auto/x") is None