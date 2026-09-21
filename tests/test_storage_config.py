"""Cloudinary configuration and failure mapping — no network."""

import pytest

from app.core.config import Settings

BASE = {
    "SECRET_KEY": "test-secret-key-at-least-thirty-two-characters-long",
    "DATABASE_URL": "postgresql://coachauto:devpass@127.0.0.1:5432/coachauto_test",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **BASE, **overrides)


def test_url_from_dotenv_is_parsed_not_passed_through():
    # Regression: the URL used to be handed to the SDK as `cloudinary_url=`,
    # which it ignores unless it came from os.environ -> "Must supply api_key".
    settings = _settings(CLOUDINARY_URL=' "cloudinary://1234:s3cr%2Bt@mycloud" ')
    assert settings.cloudinary_credentials == ("mycloud", "1234", "s3cr+t")
    assert settings.use_cloudinary


def test_separate_variables_are_accepted():
    settings = _settings(
        CLOUDINARY_CLOUD_NAME="mycloud", CLOUDINARY_API_KEY="1234", CLOUDINARY_API_SECRET="abc"
    )
    assert settings.cloudinary_credentials == ("mycloud", "1234", "abc")


def test_malformed_url_fails_loudly():
    settings = _settings(CLOUDINARY_URL="https://mycloud.example.com")
    with pytest.raises(RuntimeError, match="malformed"):
        _ = settings.cloudinary_credentials


@pytest.mark.parametrize(
    ("value", "accepted"),
    [("", False), ("254218742779793", False), ("zz12", False), ("a1b2c3d4", True)],
)
def test_auth_token_key_must_be_a_hex_token_key(value, accepted):
    settings = _settings(CLOUDINARY_AUTH_TOKEN_KEY=value)
    assert bool(settings.cloudinary_auth_token_key) is accepted


def test_failures_map_to_honest_statuses():
    import cloudinary.exceptions as cld  # noqa: PLC0415

    from app.services.storage import _storage_unavailable  # noqa: PLC0415

    assert _storage_unavailable(ValueError("Must supply api_key")).status_code == 503
    assert _storage_unavailable(cld.AuthorizationRequired("Invalid Signature")).status_code == 503
    assert _storage_unavailable(cld.BadRequest("Unsupported format")).status_code == 422
    assert _storage_unavailable(TimeoutError("timed out")).status_code == 502