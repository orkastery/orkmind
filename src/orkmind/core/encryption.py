"""Envelope encryption at-rest para OrkMind (AES-256-GCM)."""

from __future__ import annotations

import base64
import hashlib
import os
from abc import ABC, abstractmethod
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes

    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False


class EncryptionError(Exception):
    """Erro especifico de encryption/decryption."""


def encryption_available() -> bool:
    """Retorna True se a lib cryptography esta disponivel."""
    return _CRYPTO_AVAILABLE


class KeyProvider(ABC):
    """Interface para providers de chave."""

    @abstractmethod
    def get_kek(self) -> bytes:
        """Retorna KEK de 256 bits."""

    @abstractmethod
    def rotate_kek(self, old_kek: bytes, new_kek: bytes) -> None:
        """Rotaciona KEK."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Nome do provider."""


class LocalKeyProvider(KeyProvider):
    """Provider local: root key em arquivo, KEK derivada via HKDF-SHA256."""

    def __init__(self, root_key_path: str | Path = "~/.orkmind/keys/root.key") -> None:
        self._path = Path(root_key_path).expanduser()

    def get_kek(self) -> bytes:
        if not self._path.exists():
            raise EncryptionError(
                f"Root key nao encontrada: {self._path}. "
                "Execute generate_root_key() primeiro."
            )
        root_key = self._path.read_bytes()
        if not _CRYPTO_AVAILABLE:
            raise EncryptionError("Biblioteca cryptography nao instalada")
        kek = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"orkmind-kek-v1",
        ).derive(root_key)
        return kek

    def rotate_kek(self, old_kek: bytes, new_kek: bytes) -> None:
        pass

    @property
    def provider_name(self) -> str:
        return "local"

    @staticmethod
    def generate_root_key(path: str | Path = "~/.orkmind/keys/root.key") -> None:
        """Gera root key de 32 bytes random com permissao 0600."""
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        p.write_bytes(key)
        p.chmod(0o600)


class VaultKeyProvider(KeyProvider):
    """Provider HashiCorp Vault (stub)."""

    def get_kek(self) -> bytes:
        raise NotImplementedError("VaultKeyProvider ainda nao implementado")

    def rotate_kek(self, old_kek: bytes, new_kek: bytes) -> None:
        raise NotImplementedError("VaultKeyProvider ainda nao implementado")

    @property
    def provider_name(self) -> str:
        return "vault"


class KMSKeyProvider(KeyProvider):
    """Provider AWS/GCP/Azure KMS (stub)."""

    def get_kek(self) -> bytes:
        raise NotImplementedError("KMSKeyProvider ainda nao implementado")

    def rotate_kek(self, old_kek: bytes, new_kek: bytes) -> None:
        raise NotImplementedError("KMSKeyProvider ainda nao implementado")

    @property
    def provider_name(self) -> str:
        return "kms"


def encrypt_content(plaintext: str, kek: bytes) -> tuple[str, dict]:
    """Encripta conteudo com envelope encryption (KEK -> DEK -> AES-256-GCM).

    Retorna (ciphertext_b64, encryption_meta).
    """
    if not _CRYPTO_AVAILABLE:
        raise EncryptionError("Biblioteca cryptography nao instalada")

    # Gerar DEK random
    dek = os.urandom(32)
    iv = os.urandom(12)

    # Encriptar conteudo com DEK
    aesgcm = AESGCM(dek)
    ciphertext = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)

    # Encriptar DEK com KEK
    kek_iv = os.urandom(12)
    kek_aesgcm = AESGCM(kek)
    dek_encrypted = kek_aesgcm.encrypt(kek_iv, dek, None)

    # Gerar kek_id (hash parcial da KEK para identificacao)
    kek_id = hashlib.sha256(kek).hexdigest()[:16]

    meta = {
        "dek_encrypted": base64.b64encode(dek_encrypted).decode(),
        "dek_iv": base64.b64encode(kek_iv).decode(),
        "iv": base64.b64encode(iv).decode(),
        "provider": "local",
        "kek_id": kek_id,
    }

    return base64.b64encode(ciphertext).decode(), meta


def decrypt_content(ciphertext_b64: str, meta: dict, kek: bytes) -> str:
    """Decripta conteudo: KEK -> DEK -> plaintext."""
    if not _CRYPTO_AVAILABLE:
        raise EncryptionError("Biblioteca cryptography nao instalada")

    try:
        # Decriptar DEK com KEK
        dek_encrypted = base64.b64decode(meta["dek_encrypted"])
        dek_iv = base64.b64decode(meta["dek_iv"])
        kek_aesgcm = AESGCM(kek)
        dek = kek_aesgcm.decrypt(dek_iv, dek_encrypted, None)

        # Decriptar conteudo com DEK
        ciphertext = base64.b64decode(ciphertext_b64)
        iv = base64.b64decode(meta["iv"])
        aesgcm = AESGCM(dek)
        plaintext = aesgcm.decrypt(iv, ciphertext, None)
        return plaintext.decode("utf-8")
    except Exception as e:
        raise EncryptionError(f"Falha na decriptacao: {e}") from e


def rotate_keys(
    entries: list[tuple[str, dict]], old_kek: bytes, new_kek: bytes,
) -> list[tuple[str, dict]]:
    """Re-encripta DEKs com nova KEK (sem re-encriptar content).

    Recebe lista de (ciphertext_b64, meta), retorna lista atualizada.
    """
    if not _CRYPTO_AVAILABLE:
        raise EncryptionError("Biblioteca cryptography nao instalada")

    result = []
    old_aesgcm = AESGCM(old_kek)
    new_aesgcm = AESGCM(new_kek)

    for ciphertext_b64, meta in entries:
        # Decriptar DEK com KEK antiga
        dek_encrypted = base64.b64decode(meta["dek_encrypted"])
        dek_iv = base64.b64decode(meta["dek_iv"])
        dek = old_aesgcm.decrypt(dek_iv, dek_encrypted, None)

        # Re-encriptar DEK com KEK nova
        new_dek_iv = os.urandom(12)
        new_dek_encrypted = new_aesgcm.encrypt(new_dek_iv, dek, None)

        new_kek_id = hashlib.sha256(new_kek).hexdigest()[:16]
        new_meta = dict(meta)
        new_meta["dek_encrypted"] = base64.b64encode(new_dek_encrypted).decode()
        new_meta["dek_iv"] = base64.b64encode(new_dek_iv).decode()
        new_meta["kek_id"] = new_kek_id

        result.append((ciphertext_b64, new_meta))
    return result
