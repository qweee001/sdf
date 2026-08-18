from app.crypto import SecretBox


def test_encrypt_decrypt_roundtrip():
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    box = SecretBox(key)
    c = box.encrypt("some secret session")
    assert c != "some secret session"
    assert box.decrypt(c) == "some secret session"


def test_invalid_key_raises():
    import pytest

    with pytest.raises(ValueError):
        SecretBox("not-a-valid-fernet-key")


def test_fingerprint_stable():
    assert SecretBox.fingerprint("abc") == SecretBox.fingerprint("abc")
    assert SecretBox.fingerprint("abc") != SecretBox.fingerprint("abd")
