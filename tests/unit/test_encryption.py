"""Testes para encryption at-rest (D8)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from orkmind.core.encryption import (
    EncryptionError,
    LocalKeyProvider,
    decrypt_content,
    encrypt_content,
    encryption_available,
    rotate_keys,
)
from orkmind.core.injection import compute_content_hash
from orkmind.core.models import MemoryEntry


class TestEncryptionAvailability:
    def test_encryption_available_with_lib(self) -> None:
        """Retorna True quando cryptography instalado."""
        assert encryption_available() is True

    def test_encryption_import(self) -> None:
        """cryptography esta disponivel no venv."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        assert AESGCM is not None


class TestEncryptDecrypt:
    def _get_kek(self) -> bytes:
        return os.urandom(32)

    def test_encrypt_decrypt_roundtrip(self) -> None:
        """Plaintext encripta e decripta sem perda."""
        kek = self._get_kek()
        plaintext = "Conteudo secreto para teste de roundtrip"
        ciphertext, meta = encrypt_content(plaintext, kek)
        result = decrypt_content(ciphertext, meta, kek)
        assert result == plaintext

    def test_encrypt_produces_different_ciphertext(self) -> None:
        """Duas encriptacoes do mesmo texto geram ciphertext diferente (DEK random)."""
        kek = self._get_kek()
        plaintext = "Mesmo texto"
        ct1, _ = encrypt_content(plaintext, kek)
        ct2, _ = encrypt_content(plaintext, kek)
        assert ct1 != ct2

    def test_decrypt_wrong_kek_fails(self) -> None:
        """KEK errada levanta EncryptionError."""
        kek1 = os.urandom(32)
        kek2 = os.urandom(32)
        plaintext = "Texto secreto"
        ciphertext, meta = encrypt_content(plaintext, kek1)
        with pytest.raises(EncryptionError):
            decrypt_content(ciphertext, meta, kek2)

    def test_decrypt_tampered_ciphertext_fails(self) -> None:
        """Ciphertext alterado levanta EncryptionError (GCM tag)."""
        kek = self._get_kek()
        plaintext = "Texto original"
        ciphertext, meta = encrypt_content(plaintext, kek)
        # Alterar um byte do ciphertext
        import base64
        raw = base64.b64decode(ciphertext)
        tampered = bytes([raw[0] ^ 0xFF]) + raw[1:]
        tampered_b64 = base64.b64encode(tampered).decode()
        with pytest.raises(EncryptionError):
            decrypt_content(tampered_b64, meta, kek)

    def test_encrypt_meta_structure(self) -> None:
        """Meta contem dek_encrypted, iv, dek_iv, provider, kek_id."""
        kek = self._get_kek()
        _, meta = encrypt_content("test", kek)
        assert "dek_encrypted" in meta
        assert "iv" in meta
        assert "dek_iv" in meta
        assert "provider" in meta
        assert "kek_id" in meta


class TestLocalKeyProvider:
    def test_generate_root_key(self) -> None:
        """Gera arquivo com 32 bytes, permissao 0600."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = Path(tmpdir) / "root.key"
            LocalKeyProvider.generate_root_key(key_path)
            assert key_path.exists()
            assert len(key_path.read_bytes()) == 32
            assert oct(key_path.stat().st_mode)[-3:] == "600"

    def test_get_kek_deterministic(self) -> None:
        """Mesmo root key gera mesmo KEK (HKDF)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = Path(tmpdir) / "root.key"
            LocalKeyProvider.generate_root_key(key_path)
            p1 = LocalKeyProvider(key_path)
            p2 = LocalKeyProvider(key_path)
            assert p1.get_kek() == p2.get_kek()

    def test_missing_key_raises(self) -> None:
        """Arquivo inexistente levanta erro claro."""
        provider = LocalKeyProvider("/tmp/orkmind_test_nonexistent_key_12345.key")
        with pytest.raises(EncryptionError, match="nao encontrada"):
            provider.get_kek()


class TestRotateKeys:
    def test_rotate_keys_reencrypts_dek(self) -> None:
        """DEK re-encriptado com nova KEK, content nao muda."""
        old_kek = os.urandom(32)
        new_kek = os.urandom(32)
        plaintext = "Texto para rotacao"
        ct, meta = encrypt_content(plaintext, old_kek)

        rotated = rotate_keys([(ct, meta)], old_kek, new_kek)
        assert len(rotated) == 1
        new_ct, new_meta = rotated[0]
        # Ciphertext do content nao muda (so DEK foi re-encriptado)
        assert new_ct == ct
        # Meta mudou
        assert new_meta["dek_encrypted"] != meta["dek_encrypted"]
        assert new_meta["kek_id"] != meta["kek_id"]
        # Decriptar com nova KEK funciona
        result = decrypt_content(new_ct, new_meta, new_kek)
        assert result == plaintext


class TestContentHashOnPlaintext:
    def test_content_hash_on_plaintext(self) -> None:
        """Hash computado antes de encriptar, verificavel apos decrypt."""
        kek = os.urandom(32)
        plaintext = "Conteudo para hash"
        content_hash = compute_content_hash(plaintext)
        ciphertext, meta = encrypt_content(plaintext, kek)
        # Apos decrypt, hash confere
        decrypted = decrypt_content(ciphertext, meta, kek)
        assert compute_content_hash(decrypted) == content_hash


class TestMemoryEntryEncryption:
    def test_entry_encrypted_flag_default_false(self) -> None:
        """MemoryEntry novo tem encrypted=False."""
        entry = MemoryEntry(content="test", collection="fact")
        assert entry.encrypted is False
        assert entry.encryption_meta is None

    def test_old_entries_remain_plaintext(self) -> None:
        """Entries pre-existentes sem encrypted flag funcionam normal."""
        entry = MemoryEntry(content="conteudo normal", collection="fact")
        assert entry.encrypted is False
        assert entry.content == "conteudo normal"

    def test_encryption_disabled_config(self) -> None:
        """encryption_enabled=False nao encripta nada (teste de config)."""
        from orkmind.core.config import OrkMindConfig
        cfg = OrkMindConfig()
        assert cfg.encryption_enabled is False
        assert cfg.encryption_collections is None
