import pytest

from efficharge.crypto import DecryptionError, Vault, account_aad, generate_key


def test_roundtrip_and_no_plaintext(vault):
    token = vault.encrypt({"token": "secret-abc"}, account_aad(1, "meross"))
    assert "secret-abc" not in token
    assert token.startswith("v1:k1:")
    assert vault.decrypt(token, account_aad(1, "meross")) == {"token": "secret-abc"}


def test_ciphertext_bound_to_its_row(vault):
    token = vault.encrypt({"token": "x"}, account_aad(1, "meross"))
    with pytest.raises(DecryptionError):
        vault.decrypt(token, account_aad(2, "meross"))
    with pytest.raises(DecryptionError):
        vault.decrypt(token, account_aad(1, "shelly_cloud"))


def test_tampering_detected(vault):
    token = vault.encrypt({"token": "x"}, "a")
    flipped = token[:-3] + ("A" if token[-3] != "A" else "B") + token[-2:]
    with pytest.raises(DecryptionError):
        vault.decrypt(flipped, "a")


def test_key_rotation():
    old_key, new_key = generate_key(), generate_key()
    old = Vault.from_config(f"k1:{old_key}")
    token = old.encrypt({"t": 1}, "a")
    rotated = Vault.from_config(f"k2:{new_key},k1:{old_key}")
    assert rotated.decrypt(token, "a") == {"t": 1}
    assert rotated.needs_rotation(token)
    assert not rotated.needs_rotation(rotated.encrypt({"t": 1}, "a"))


def test_unknown_key_and_bad_config():
    token = Vault.from_config(f"k1:{generate_key()}").encrypt({}, "a")
    with pytest.raises(DecryptionError):
        Vault.from_config(f"k1:{generate_key()}").decrypt(token, "a")
    with pytest.raises(ValueError):
        Vault.from_config("")
    with pytest.raises(ValueError):
        Vault.from_config("k1:c2hvcnQ=")  # 5 bytes
