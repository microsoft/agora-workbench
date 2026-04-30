"""Tests for IRM detection and decryption."""

import struct
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# irm.py imports olefile lazily (inside functions). We need the module itself
# importable so we can reference its helpers. Since olefile isn't installed in
# the dev venv, we insert a lightweight stub into sys.modules before importing.
_olefile_stub = MagicMock()
_olefile_stub.isOleFile = MagicMock(return_value=False)
_olefile_stub.OleFileIO = MagicMock()
sys.modules.setdefault("olefile", _olefile_stub)

from ...code_execution.auth.irm import (  # noqa: E402
    IRMDecryptionError,
    is_irm_protected,
    decrypt_irm_file,
    _decrypt_content,
    _parse_publishing_license,
    _extract_rms_url,
)

# Reference to the irm module for patching
from ...code_execution.auth import irm as _irm_module  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers to build fake OLE2 files
# ---------------------------------------------------------------------------


def _ole2_header() -> bytes:
    """Return the 8-byte OLE2 magic header."""
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class FakeOleFile:
    """Minimal fake for ``olefile.OleFileIO``."""

    def __init__(self, streams: dict[str, bytes]):
        self._streams = streams

    def exists(self, name: str) -> bool:
        return name in self._streams

    def openstream(self, name: str):
        m = MagicMock()
        m.read.return_value = self._streams[name]
        return m

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# is_irm_protected
# ---------------------------------------------------------------------------


class TestIsIrmProtected:
    """Tests for ``is_irm_protected``."""

    def test_non_ole2_file(self, tmp_path):
        """Non-OLE2 files are not IRM-protected."""
        f = tmp_path / "plain.xlsx"
        f.write_bytes(b"PK\x03\x04notanolefile")  # ZIP-based OOXML
        assert is_irm_protected(f) is False

    def test_ole2_without_drm_streams(self, tmp_path):
        """OLE2 file without DRM streams is not IRM-protected."""
        f = tmp_path / "normal.doc"
        f.write_bytes(_ole2_header() + b"\x00" * 1024)

        fake_ole = FakeOleFile({})
        mock_olefile = MagicMock()
        mock_olefile.isOleFile = MagicMock(return_value=True)
        mock_olefile.OleFileIO = MagicMock(return_value=fake_ole)
        with patch.dict(sys.modules, {"olefile": mock_olefile}):
            assert is_irm_protected(f) is False

    def test_ole2_with_drm_streams(self, tmp_path):
        """OLE2 file with DRM streams is IRM-protected."""
        f = tmp_path / "protected.xlsx"
        f.write_bytes(_ole2_header() + b"\x00" * 512)

        fake_ole = FakeOleFile(
            {
                "\x09DRMContent": b"encrypted",
                "\x06DataSpaces": b"header",
            }
        )
        mock_olefile = MagicMock()
        mock_olefile.isOleFile = MagicMock(return_value=True)
        mock_olefile.OleFileIO = MagicMock(return_value=fake_ole)
        with patch.dict(sys.modules, {"olefile": mock_olefile}):
            assert is_irm_protected(f) is True

    def test_missing_file(self, tmp_path):
        """Missing file returns False instead of raising."""
        assert is_irm_protected(tmp_path / "missing.xlsx") is False

    def test_corrupt_file(self, tmp_path):
        """Corrupt file returns False instead of raising."""
        f = tmp_path / "corrupt.bin"
        f.write_bytes(b"\x00")
        assert is_irm_protected(f) is False


# ---------------------------------------------------------------------------
# _decrypt_content  (pure crypto, no network)
# ---------------------------------------------------------------------------


class TestDecryptContent:
    """Tests for AES-CBC decryption of the content stream."""

    def _encrypt(self, plaintext: bytes, key: bytes, iv: bytes) -> bytes:
        """AES-CBC encrypt with PKCS7 padding, prepend 8-byte header."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as sym_padding

        padder = sym_padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        enc = cipher.encryptor()
        ciphertext = enc.update(padded) + enc.finalize()
        header = struct.pack("<I", len(plaintext)) + b"\x00\x00\x00\x00"
        return header + ciphertext

    def test_roundtrip(self):
        """Encrypt then decrypt should return original content."""
        key = b"\x01" * 16
        iv = b"\x02" * 16
        original = b"Hello, World! This is a test document."
        encrypted = self._encrypt(original, key, iv)
        result = _decrypt_content(encrypted, key, iv)
        assert result == original

    def test_empty_ciphertext_raises(self):
        """Empty ciphertext (just header) should raise."""
        header = struct.pack("<I", 0) + b"\x00\x00\x00\x00"
        with pytest.raises(IRMDecryptionError, match="No ciphertext"):
            _decrypt_content(header, b"\x00" * 16, b"\x00" * 16)

    def test_too_short_raises(self):
        """Content shorter than 8 bytes should raise."""
        with pytest.raises(IRMDecryptionError, match="too short"):
            _decrypt_content(b"\x00\x00", b"\x00" * 16, b"\x00" * 16)


# ---------------------------------------------------------------------------
# _parse_publishing_license
# ---------------------------------------------------------------------------


class TestParsePublishingLicense:
    """Tests for extracting XrML from the transform info stream."""

    def test_utf16le_xrml(self):
        """Should extract XrML from UTF-16LE encoded stream."""
        xrml = "<XrML><Body>license</Body></XrML>"
        # Prepend a fake binary header
        raw = b"\x00" * 20 + xrml.encode("utf-16-le")
        result = _parse_publishing_license(raw)
        assert b"<XrML>" in result
        assert b"license" in result

    def test_utf8_xrml_fallback(self):
        """Should extract XrML from UTF-8 stream when no UTF-16LE marker."""
        xrml = b"\x00" * 10 + b"<XrML><Body>license</Body></XrML>"
        result = _parse_publishing_license(xrml)
        assert b"<XrML>" in result

    def test_no_xrml_raises(self):
        """Should raise when no XrML is found."""
        with pytest.raises(IRMDecryptionError, match="Could not locate XrML"):
            _parse_publishing_license(b"\x00" * 100)


# ---------------------------------------------------------------------------
# _extract_rms_url
# ---------------------------------------------------------------------------


class TestExtractRmsUrl:
    """Tests for extracting the RMS URL from a publishing license."""

    def test_extracts_aadrm_url(self):
        """Should extract an aadrm.com URL from the license XML."""
        xml = b"""<XrML>
            <BODY>
                <DISTRIBUTIONPOINT>
                    <OBJECT type="License-Acquisition-URL">
                        <ADDRESS type="URL">https://contoso.aadrm.com/_wmcs/licensing/Publish.asmx</ADDRESS>
                    </OBJECT>
                </DISTRIBUTIONPOINT>
            </BODY>
        </XrML>"""
        url = _extract_rms_url(xml)
        assert url == "https://contoso.aadrm.com"

    def test_no_url_raises(self):
        """Should raise when no RMS URL is found."""
        xml = b"<XrML><BODY><NOTHING/></BODY></XrML>"
        with pytest.raises(IRMDecryptionError, match="Could not extract RMS"):
            _extract_rms_url(xml)


# ---------------------------------------------------------------------------
# decrypt_irm_file (end-to-end with mocked RMS)
# ---------------------------------------------------------------------------


class TestDecryptIrmFile:
    """End-to-end decryption with mocked OLE2 parsing and RMS API."""

    @pytest.mark.asyncio
    async def test_decrypt_success(self, tmp_path):
        """Full pipeline: extract → acquire key → decrypt → write."""
        key = b"\xaa" * 16
        iv = b"\xbb" * 16
        original = b"decrypted office content here"

        # Build fake encrypted content
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as sym_padding

        padder = sym_padding.PKCS7(128).padder()
        padded = padder.update(original) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        enc = cipher.encryptor()
        ciphertext = enc.update(padded) + enc.finalize()
        encrypted_content = struct.pack("<I", len(original)) + b"\x00\x00\x00\x00" + ciphertext

        pub_license = b"<XrML><BODY>fake</BODY></XrML>"

        input_file = tmp_path / "input.xlsx"
        input_file.write_bytes(b"placeholder")
        output_file = tmp_path / "output.xlsx"

        with (
            patch.object(
                _irm_module,
                "_extract_irm_data",
                return_value=(pub_license, encrypted_content),
            ),
            patch.object(
                _irm_module,
                "_acquire_content_key",
                new_callable=AsyncMock,
                return_value=(key, iv),
            ),
        ):
            await decrypt_irm_file(input_file, output_file, "fake-token")

        assert output_file.exists()
        assert output_file.read_bytes() == original

    @pytest.mark.asyncio
    async def test_decrypt_extract_failure(self, tmp_path):
        """Should wrap extraction errors in IRMDecryptionError."""
        f = tmp_path / "bad.xlsx"
        f.write_bytes(b"x")

        with patch.object(
            _irm_module,
            "_extract_irm_data",
            side_effect=ValueError("bad file"),
        ):
            with pytest.raises(IRMDecryptionError, match="Failed to read IRM data"):
                await decrypt_irm_file(f, f, "token")

    @pytest.mark.asyncio
    async def test_decrypt_rms_failure(self, tmp_path):
        """Should wrap RMS errors in IRMDecryptionError."""
        f = tmp_path / "test.xlsx"
        f.write_bytes(b"x")

        with (
            patch.object(
                _irm_module,
                "_extract_irm_data",
                return_value=(b"<XrML/>", b"\x00" * 100),
            ),
            patch.object(
                _irm_module,
                "_acquire_content_key",
                new_callable=AsyncMock,
                side_effect=RuntimeError("RMS unavailable"),
            ),
        ):
            with pytest.raises(IRMDecryptionError, match="Failed to acquire content key"):
                await decrypt_irm_file(f, f, "token")
