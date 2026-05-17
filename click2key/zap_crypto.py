"""Zwift ZAP protocol crypto: ECDH P-256 + HKDF-SHA256 + AES-CCM.

Reference: https://github.com/ajchellew/zwiftplay (C# + Kotlin ports).

Handshake (host → puck on SYNC_TX):
    RIDE_ON || REQUEST_START || local_pub(64)

Handshake reply (puck → host on SYNC_RX):
    RIDE_ON || RESPONSE_START || device_pub(64)

Key derivation:
    salt = device_pub || local_pub                 (128 bytes)
    ikm  = ECDH(local_priv, device_pub)            (raw X coord)
    HKDF-SHA256(ikm, salt) → aes_key(32) || iv_prefix(4)

Encrypted frame (puck → host on ASYNC):
    counter(4 big-endian) || ciphertext(N) || tag(4)
    nonce = iv_prefix || counter                   (8 bytes)
    AES-CCM with 4-byte tag, no associated data.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RIDE_ON = b"RideOn"
REQUEST_START = b"\x00\x09"
RESPONSE_START = b"\x01\x03"
MAC_LENGTH = 4
HKDF_LENGTH = 36
KEY_LENGTH = 32
PUBKEY_RAW_LENGTH = 64  # uncompressed P-256 without the leading 0x04 byte


class ZapKeyExchange:
    """Generates an ephemeral P-256 keypair and derives the AES key + IV.

    Holds the private key for the lifetime of the BLE link; the cipher
    parameters are derived once on receipt of the device's public key.
    """

    def __init__(self) -> None:
        self._private = ec.generate_private_key(ec.SECP256R1())
        self._public_raw = _serialize_pub(self._private.public_key())

    def local_pub_bytes(self) -> bytes:
        """64-byte raw X||Y (no 0x04 prefix), as expected by the puck."""
        return self._public_raw

    def derive(self, device_pub_bytes: bytes) -> tuple[bytes, bytes]:
        if len(device_pub_bytes) != PUBKEY_RAW_LENGTH:
            raise ValueError(
                f"device pubkey must be {PUBKEY_RAW_LENGTH} bytes, got {len(device_pub_bytes)}"
            )
        device_pub = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), b"\x04" + device_pub_bytes,
        )
        shared = self._private.exchange(ec.ECDH(), device_pub)
        salt = device_pub_bytes + self._public_raw
        hkdf_out = HKDF(
            algorithm=hashes.SHA256(),
            length=HKDF_LENGTH,
            salt=salt,
            info=None,
        ).derive(shared)
        return hkdf_out[:KEY_LENGTH], hkdf_out[KEY_LENGTH:]


def _serialize_pub(pub: ec.EllipticCurvePublicKey) -> bytes:
    encoded = pub.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    # Strip the 0x04 uncompressed-point marker; the wire format is raw X||Y.
    assert encoded[:1] == b"\x04" and len(encoded) == 1 + PUBKEY_RAW_LENGTH
    return encoded[1:]


class ZapCipher:
    """AES-CCM decrypter for ASYNC notifications. Initialised post-handshake."""

    def __init__(self, aes_key: bytes, iv_prefix: bytes) -> None:
        if len(aes_key) != KEY_LENGTH:
            raise ValueError(f"AES key must be {KEY_LENGTH} bytes")
        if len(iv_prefix) != HKDF_LENGTH - KEY_LENGTH:
            raise ValueError(
                f"IV prefix must be {HKDF_LENGTH - KEY_LENGTH} bytes"
            )
        self._ccm = AESCCM(aes_key, tag_length=MAC_LENGTH)
        self._iv_prefix = iv_prefix

    def decrypt(self, counter_bytes: bytes, ciphertext_with_tag: bytes) -> bytes:
        if len(counter_bytes) != 4:
            raise ValueError("counter must be 4 bytes")
        nonce = self._iv_prefix + counter_bytes
        return self._ccm.decrypt(nonce, ciphertext_with_tag, None)

    def encrypt(self, counter_bytes: bytes, plaintext: bytes) -> bytes:
        """Only used in tests; the host never sends encrypted frames."""
        if len(counter_bytes) != 4:
            raise ValueError("counter must be 4 bytes")
        nonce = self._iv_prefix + counter_bytes
        return self._ccm.encrypt(nonce, plaintext, None)
