"""Encryption helpers for socket rendezvous information."""

import base64
import json
import os
from pathlib import Path

import nacl.exceptions
import nacl.public


CONNECTION_FILE_FORMAT = "lapd-curve25519-sealed-box-v1"


def load_public_key(path):
    try:
        encoded = Path(path).read_bytes()
        return nacl.public.PublicKey(encoded)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Unable to load Curve25519 public key from {path}: {exc}"
        ) from exc


def load_private_key(path):
    try:
        encoded = Path(path).read_bytes()
        return nacl.public.PrivateKey(encoded)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Unable to load Curve25519 private key from {path}: {exc}"
        ) from exc


def encrypt_connection_info(connection_info, public_key):
    plaintext = json.dumps(connection_info, separators=(",", ":")).encode("utf-8")
    ciphertext = nacl.public.SealedBox(public_key).encrypt(plaintext)
    envelope = {
        "format": CONNECTION_FILE_FORMAT,
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(envelope, indent=2) + "\n"


def decrypt_connection_info(envelope, private_key):
    if (
        not isinstance(envelope, dict)
        or envelope.get("format") != CONNECTION_FILE_FORMAT
    ):
        raise ValueError("Connection file is not encrypted in the supported format")
    try:
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        plaintext = nacl.public.SealedBox(private_key).decrypt(ciphertext)
        connection_info = json.loads(plaintext.decode("utf-8"))
    except (KeyError, TypeError, ValueError, nacl.exceptions.CryptoError) as exc:
        raise ValueError(
            "Unable to decrypt connection information with the private key"
        ) from exc
    if not isinstance(connection_info, dict) or "id" not in connection_info:
        raise ValueError("Decrypted connection information is invalid")
    return connection_info


def write_encrypted_connection_file(path, connection_info, public_key):
    path = Path(path)
    contents = encrypt_connection_info(connection_info, public_key)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(contents)
    os.replace(temporary, path)
    return contents


def remove_connection_file_if_unchanged(path, expected_contents):
    if expected_contents is None:
        return
    path = Path(path)
    try:
        if path.read_text(encoding="utf-8") == expected_contents:
            path.unlink()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        pass
