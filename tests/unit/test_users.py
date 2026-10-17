"""Credential and user-invariant tests."""

import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from valutatrade_hub.core.exceptions import ValidationError
from valutatrade_hub.core.models import User


def test_password_uses_salted_sha256_and_verifies(user):
    """Stored credentials follow the assignment's hash contract."""
    expected = hashlib.sha256(("secret" + user.salt).encode("utf-8")).hexdigest()
    assert user.hashed_password == expected
    assert user.verify_password("secret")
    assert not user.verify_password("wrong")
    assert not user.verify_password("")
    assert not user.verify_password(None)


def test_equal_passwords_have_different_salts(user):
    """Users do not share the same salted credential."""
    other = User.create(2, "bob", "secret")
    assert user.salt != other.salt
    assert user.hashed_password != other.hashed_password


def test_reconstruct_user_with_saved_credentials(user):
    """Restoring credentials does not rehash an already hashed password."""
    restored = User(
        user.user_id,
        user.username,
        user.hashed_password,
        user.salt,
        user.registration_date,
    )
    assert restored.get_user_info() == user.get_user_info()
    assert restored.verify_password("secret")


def test_password_change_replaces_salt_and_invalidates_old_password(user):
    """A password change replaces both stored credential components."""
    old_credentials = user.salt, user.hashed_password
    user.change_password("новый пароль")
    assert user.salt != old_credentials[0]
    assert user.hashed_password != old_credentials[1]
    assert user.verify_password("новый пароль")
    assert not user.verify_password("secret")


@pytest.mark.parametrize("password", ["", "abc", 1234, None, True])
def test_invalid_password_does_not_change_credentials(user, password):
    """Rejected password changes leave valid saved credentials intact."""
    old_credentials = user.salt, user.hashed_password
    with pytest.raises(ValidationError, match="не короче 4"):
        user.change_password(password)
    assert (user.salt, user.hashed_password) == old_credentials
    with pytest.raises(ValidationError, match="не короче 4"):
        User.create(2, "bob", password)


def test_password_whitespace_is_significant():
    """Password validation does not strip or normalize a supplied password."""
    account = User.create(1, "alice", " ab ")
    assert account.verify_password(" ab ")
    assert not account.verify_password("ab")
    assert User.create(2, "bob", "    ").verify_password("    ")


@pytest.mark.parametrize("username", ["", " \t", None, 123, True])
def test_invalid_username_does_not_replace_existing_name(user, username):
    """Username setters preserve the previous value after invalid input."""
    with pytest.raises(ValidationError, match="Имя пользователя"):
        user.username = username
    assert user.username == "alice"


def test_username_trims_outer_whitespace_and_keeps_case(user):
    """Names preserve case and Unicode while trimming surrounding space."""
    user.username = "  Алиса  "
    assert user.username == "Алиса"


@pytest.mark.parametrize("user_id", [0, -1, True, False, 1.0, "1", None])
def test_user_id_requires_positive_integer(user_id):
    """Invalid identifiers cannot enter newly created domain objects."""
    with pytest.raises(ValidationError, match="Идентификатор"):
        User.create(user_id, "alice", "secret")


def test_public_information_omits_credentials_and_cannot_mutate_user(user):
    """The public view is safe to return and independent of the user object."""
    info = user.get_user_info()
    assert info == {
        "user_id": 1,
        "username": "alice",
        "registration_date": "2026-10-17T00:00:00+00:00",
    }
    info["username"] = "bob"
    assert user.username == "alice"
    with pytest.raises(AttributeError):
        user.user_id = 2


def test_registration_date_is_normalized_to_utc():
    """A timezone-aware timestamp keeps its instant when stored as UTC."""
    original = datetime(2026, 10, 17, 12, tzinfo=timezone(timedelta(hours=3)))
    account = User.create(1, "alice", "secret", original)
    assert account.registration_date == datetime(2026, 10, 17, 9, tzinfo=UTC)
    assert account.registration_date.tzinfo is UTC


@pytest.mark.parametrize("timestamp", ["2026-10-17", datetime(2026, 10, 17)])
def test_invalid_registration_timestamp_is_rejected(timestamp):
    """Registration timestamps must be aware datetime values."""
    with pytest.raises(ValidationError, match="Дата регистрации"):
        User.create(1, "alice", "secret", timestamp)


@pytest.mark.parametrize("hashed_password", ["", "abc", "z" * 64, None])
def test_restored_hash_must_be_sha256(user, hashed_password):
    """Malformed persisted hashes fail at the model boundary."""
    with pytest.raises(ValidationError, match="Хеш пароля"):
        User(1, "alice", hashed_password, user.salt, user.registration_date)


@pytest.mark.parametrize("salt", ["", None, 12])
def test_restored_salt_must_be_nonempty_text(user, salt):
    """Restoring a user requires the original nonempty salt."""
    with pytest.raises(ValidationError, match="Соль"):
        User(1, "alice", user.hashed_password, salt, user.registration_date)
