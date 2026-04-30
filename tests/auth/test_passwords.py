"""Unit tests for password hashing helpers."""

from src.api.auth.passwords import hash_password, needs_rehash, verify_password


def test_hash_and_verify_roundtrip():
    h = hash_password("supersecretpassword")
    assert verify_password("supersecretpassword", h)


def test_wrong_password_returns_false():
    h = hash_password("correct")
    assert not verify_password("incorrect", h)


def test_empty_password_returns_false():
    h = hash_password("something")
    assert not verify_password("", h)


def test_garbage_hash_returns_false():
    assert not verify_password("password", "not-a-valid-hash")


def test_needs_rehash_fresh_hash_is_false():
    h = hash_password("password")
    assert not needs_rehash(h)
