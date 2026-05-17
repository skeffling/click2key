from click2key.zap_crypto import (
    HKDF_LENGTH,
    KEY_LENGTH,
    PUBKEY_RAW_LENGTH,
    ZapCipher,
    ZapKeyExchange,
)


def test_local_pub_bytes_length():
    kx = ZapKeyExchange()
    assert len(kx.local_pub_bytes()) == PUBKEY_RAW_LENGTH


def test_derive_yields_key_and_iv_of_expected_length():
    # Use two independent exchanges; either side can derive against the
    # other's pubkey and the lengths must always match the protocol spec.
    host = ZapKeyExchange()
    peer = ZapKeyExchange()
    aes_key, iv_prefix = host.derive(peer.local_pub_bytes())
    assert len(aes_key) == KEY_LENGTH
    assert len(iv_prefix) == HKDF_LENGTH - KEY_LENGTH == 4


def test_cipher_round_trip_with_4_byte_tag():
    # Sanity-check our nonce assembly (iv_prefix || counter) and the
    # 4-byte AES-CCM tag length. Encrypting then decrypting with the
    # same counter must yield the original plaintext.
    host = ZapKeyExchange()
    peer = ZapKeyExchange()
    aes_key, iv_prefix = host.derive(peer.local_pub_bytes())
    cipher = ZapCipher(aes_key, iv_prefix)
    counter = (42).to_bytes(4, "big")
    plaintext = b"\x07hello buttons"
    ciphertext = cipher.encrypt(counter, plaintext)
    assert cipher.decrypt(counter, ciphertext) == plaintext
