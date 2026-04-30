"""
IRM (Information Rights Management) decryption for Microsoft Office files.

Detects and decrypts IRM-protected OLE2 Office documents using an Azure RMS
access token obtained via OBO token exchange. The decrypted content is written
to disk so downstream tools can read it as a normal Office file.

IRM-protected files are OLE2 compound documents with a ``\\x09DRMContent``
stream containing AES-CBC encrypted data and a ``\\x06DataSpaces`` tree
holding the RMS publishing license needed to acquire the content key.

References:
- [MS-OFFDI] Office Document Cryptography Structure
- [MS-RMPR]  Rights Management Services Protocol

Dependencies: ``olefile``, ``cryptography``, ``httpx``
"""

import logging
import struct
from pathlib import Path
from xml.etree import ElementTree

LOGGER = logging.getLogger(__name__)

# OLE2 streams used by IRM-protected files
_DRM_CONTENT_STREAM = "\x09DRMContent"
_DRM_DATA_SPACES = "\x06DataSpaces"
_DRM_TRANSFORM_INFO = "\x06DataSpaces/TransformInfo/DRMEncryptedTransform/\x06Primary"
_DRM_VERSION_STREAM = "\x06DataSpaces/Version"

# RMS server-side license acquisition endpoint path
_RMS_LICENSE_PATH = "/_wmcs/licensing/ServerLicensing.asmx"


class IRMDecryptionError(Exception):
    """Raised when IRM decryption fails."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_irm_protected(file_path: Path) -> bool:
    """
    Check whether an OLE2 file is IRM-protected.

    Looks for the ``\\x09DRMContent`` and ``\\x06DataSpaces`` streams that
    indicate Microsoft IRM encryption.

    Args:
        file_path: Path to the file to inspect.

    Returns:
        ``True`` if the file appears to be IRM-protected, ``False`` otherwise.
    """
    try:
        import olefile

        if not olefile.isOleFile(str(file_path)):
            return False

        with olefile.OleFileIO(str(file_path)) as ole:
            has_drm_content = ole.exists(_DRM_CONTENT_STREAM)
            has_data_spaces = ole.exists(_DRM_DATA_SPACES)
            return has_drm_content and has_data_spaces
    except Exception as e:
        LOGGER.debug(f"Error checking IRM protection for {file_path}: {e}")
        return False


async def decrypt_irm_file(
    input_path: Path,
    output_path: Path,
    access_token: str,
) -> None:
    """
    Decrypt an IRM-protected Office file.

    Extracts the RMS publishing license from the OLE2 structure, calls the
    Azure RMS licensing service to acquire a use license with the content
    key, then decrypts the content stream using AES-CBC.

    Args:
        input_path:   Path to the IRM-protected file.
        output_path:  Where to write the decrypted content.  May be the same
                      as *input_path* for in-place decryption.
        access_token: Azure RMS bearer token (``https://aadrm.com/.default``
                      scope) obtained via OBO.

    Raises:
        IRMDecryptionError: If any step of the decryption pipeline fails.
    """
    # --- 1. Extract publishing license and encrypted content from OLE2 ---
    try:
        publishing_license, encrypted_content = _extract_irm_data(input_path)
    except Exception as e:
        raise IRMDecryptionError(f"Failed to read IRM data from {input_path.name}: {e}") from e

    # --- 2. Acquire use license (contains the AES content key) from RMS ---
    try:
        content_key, iv = await _acquire_content_key(publishing_license, access_token)
    except Exception as e:
        raise IRMDecryptionError(f"Failed to acquire content key from RMS: {e}") from e

    # --- 3. Decrypt the content ---
    try:
        decrypted = _decrypt_content(encrypted_content, content_key, iv)
    except Exception as e:
        raise IRMDecryptionError(f"AES decryption failed: {e}") from e

    # --- 4. Write decrypted content to output ---
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(decrypted)
    LOGGER.info(f"Decrypted IRM file written to {output_path} ({len(decrypted)} bytes)")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_irm_data(file_path: Path) -> tuple[bytes, bytes]:
    """
    Extract the RMS publishing license and encrypted content from an
    IRM-protected OLE2 file.

    Returns:
        Tuple of (publishing_license_xml_bytes, encrypted_content_bytes).
    """
    import olefile

    with olefile.OleFileIO(str(file_path)) as ole:
        # Read encrypted content
        if not ole.exists(_DRM_CONTENT_STREAM):
            raise IRMDecryptionError(f"Missing {_DRM_CONTENT_STREAM!r} stream")
        encrypted_content = ole.openstream(_DRM_CONTENT_STREAM).read()

        # Read the transform info which contains the publishing license
        if not ole.exists(_DRM_TRANSFORM_INFO):
            raise IRMDecryptionError(f"Missing {_DRM_TRANSFORM_INFO!r} stream")
        transform_raw = ole.openstream(_DRM_TRANSFORM_INFO).read()

    # The transform info stream has a binary header followed by UTF-16LE XML.
    # Parse out the publishing license XML.
    publishing_license = _parse_publishing_license(transform_raw)
    return publishing_license, encrypted_content


def _parse_publishing_license(transform_raw: bytes) -> bytes:
    """
    Parse the publishing license XML from the DRM transform info stream.

    The stream layout (per [MS-OFFDI] §2.2) is:
      - 4-byte version
      - Variable-length header with class ID and name
      - Remaining bytes are the serialized publishing license (UTF-16LE XML)

    We search for the ``<XrML`` marker that starts the license.
    """
    # Look for XrML start tag in UTF-16LE
    xrml_marker = b"<\x00X\x00r\x00M\x00L\x00"
    idx = transform_raw.find(xrml_marker)
    if idx == -1:
        # Fallback: look for plain UTF-8 XrML
        xrml_marker_utf8 = b"<XrML"
        idx = transform_raw.find(xrml_marker_utf8)
        if idx == -1:
            raise IRMDecryptionError("Could not locate XrML publishing license in transform info stream")
        return transform_raw[idx:]

    # Found UTF-16LE — decode and re-encode as UTF-8 for the SOAP request
    try:
        license_text = transform_raw[idx:].decode("utf-16-le").rstrip("\x00")
        return license_text.encode("utf-8")
    except UnicodeDecodeError:
        # If decoding fails, return raw bytes from the marker
        return transform_raw[idx:]


async def _acquire_content_key(
    publishing_license: bytes,
    access_token: str,
) -> tuple[bytes, bytes]:
    """
    Call the Azure RMS licensing endpoint to exchange the publishing license
    for a use license containing the AES content key.

    Returns:
        Tuple of (aes_key_bytes, iv_bytes).
    """
    # Extract the RMS server URL from the publishing license
    rms_url = _extract_rms_url(publishing_license)
    license_url = rms_url.rstrip("/") + _RMS_LICENSE_PATH

    # Build SOAP request for AcquireLicense
    soap_body = _build_acquire_license_soap(publishing_license)

    headers = {
        "Content-Type": "application/soap+xml; charset=utf-8",
        "Authorization": f"Bearer {access_token}",
    }

    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(license_url, content=soap_body, headers=headers)

    if response.status_code != 200:
        raise IRMDecryptionError(f"RMS license acquisition failed (HTTP {response.status_code}): {response.text[:500]}")

    # Parse the use license from the SOAP response
    return _parse_use_license_response(response.content)


def _extract_rms_url(publishing_license: bytes) -> str:
    """
    Extract the RMS licensing URL from the XrML publishing license.

    Looks for the ``<DISTRIBUTIONPOINT>`` element with an
    ``<OBJECT type="License-Acquisition-URL">`` child.
    """
    license_str = publishing_license.decode("utf-8", errors="replace")

    try:
        # Strip any trailing garbage after closing tag
        end_idx = license_str.rfind("</XrML>")
        if end_idx != -1:
            license_str = license_str[: end_idx + len("</XrML>")]

        root = ElementTree.fromstring(license_str)
    except ElementTree.ParseError as e:
        raise IRMDecryptionError(f"Failed to parse publishing license XML: {e}") from e

    # Search for the license acquisition URL in various XrML structures
    # The namespace varies, so search broadly
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag.upper() == "ADDRESS" or tag.upper() == "VALUE":
            text = (elem.text or "").strip()
            if text.startswith("https://") and "licensing" in text.lower():
                # Extract base URL (up to the first path component after host)
                from urllib.parse import urlparse

                parsed = urlparse(text)
                return f"{parsed.scheme}://{parsed.netloc}"

    # Fallback: look for any https URL resembling an RMS endpoint
    import re

    urls = re.findall(r"https://[a-zA-Z0-9._-]+\.aadrm\.com", license_str)
    if urls:
        return urls[0]

    urls = re.findall(r"https://[a-zA-Z0-9._-]+\.rms\.[a-zA-Z0-9._-]+", license_str)
    if urls:
        return urls[0]

    raise IRMDecryptionError(
        "Could not extract RMS licensing URL from publishing license. "
        "The file may use an unsupported IRM configuration."
    )


def _build_acquire_license_soap(publishing_license: bytes) -> bytes:
    """
    Build the SOAP XML envelope for an AcquireLicense request.

    The request sends the publishing license to the RMS server and requests
    a use license (containing the content key) for the authenticated user.
    """
    # Escape XML special characters in the publishing license
    license_str = publishing_license.decode("utf-8", errors="replace")
    # Wrap in CDATA to avoid escaping issues
    soap = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
               xmlns:wsa="http://www.w3.org/2005/08/addressing">
  <soap:Header>
    <wsa:Action>http://microsoft.com/DRM/LicensingService/AcquireLicense</wsa:Action>
  </soap:Header>
  <soap:Body>
    <AcquireLicense xmlns="http://microsoft.com/DRM/LicensingService">
      <RequestParams>
        <LicenseeCerts/>
        <IssuanceLicense><![CDATA[{license_str}]]></IssuanceLicense>
      </RequestParams>
    </AcquireLicense>
  </soap:Body>
</soap:Envelope>"""
    return soap.encode("utf-8")


def _parse_use_license_response(response_body: bytes) -> tuple[bytes, bytes]:
    """
    Parse the AcquireLicense SOAP response to extract the AES content key
    and IV.

    The use license contains a ``<CONTENTKEY>`` element with the base64-encoded
    AES key material and an ``<IV>`` or embedded IV.

    Returns:
        Tuple of (aes_key_bytes, iv_bytes).
    """
    import base64

    response_str = response_body.decode("utf-8", errors="replace")

    try:
        # Strip SOAP envelope to find the use license XrML
        root = ElementTree.fromstring(response_str)
    except ElementTree.ParseError:
        # Response might have namespace issues — try extracting XrML directly
        root = _extract_xml_from_soap(response_str)

    # Search for content key in the response
    key_value = None
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag

        # Look for the content key value
        if tag.upper() == "CONTENTKEY" or tag.upper() == "VALUE":
            val = (elem.text or "").strip()
            if val and len(val) > 10:
                try:
                    decoded = base64.b64decode(val)
                    if len(decoded) >= 16:
                        key_value = decoded
                        break
                except Exception:
                    continue

        # Check attributes too
        for attr_val in elem.attrib.values():
            if attr_val and len(attr_val) > 20:
                try:
                    decoded = base64.b64decode(attr_val)
                    if len(decoded) >= 16:
                        key_value = decoded
                        break
                except Exception:
                    continue

    if key_value is None:
        raise IRMDecryptionError("Could not extract content key from RMS use license response")

    # The key material may be: key_only (16 bytes) or key+iv (32 bytes)
    if len(key_value) >= 32:
        aes_key = key_value[:16]
        iv = key_value[16:32]
    elif len(key_value) == 16:
        aes_key = key_value
        iv = b"\x00" * 16  # Default IV if not provided separately
    else:
        raise IRMDecryptionError(f"Unexpected content key length: {len(key_value)} bytes (expected 16 or 32)")

    LOGGER.debug(f"Extracted AES-128 content key ({len(aes_key)} bytes) and IV ({len(iv)} bytes)")
    return aes_key, iv


def _extract_xml_from_soap(response_str: str) -> ElementTree.Element:
    """
    Fallback parser: extract the inner XML from a SOAP response body
    when standard parsing fails due to namespace issues.
    """
    import re

    # Try to find an XrML block
    xrml_match = re.search(r"<XrML[\s\S]*?</XrML>", response_str)
    if xrml_match:
        try:
            return ElementTree.fromstring(xrml_match.group())
        except ElementTree.ParseError:
            pass

    # Try the SOAP body
    body_match = re.search(r"<(?:\w+:)?Body[^>]*>([\s\S]*?)</(?:\w+:)?Body>", response_str)
    if body_match:
        try:
            return ElementTree.fromstring(body_match.group(1).strip())
        except ElementTree.ParseError:
            pass

    raise IRMDecryptionError("Could not parse RMS SOAP response")


def _decrypt_content(
    encrypted_content: bytes,
    aes_key: bytes,
    iv: bytes,
) -> bytes:
    """
    Decrypt the ``\\x09DRMContent`` stream using AES-128-CBC.

    The encrypted content has a small header (8 bytes: 4-byte length + 4-byte
    flags) followed by AES-CBC encrypted blocks.

    Args:
        encrypted_content: Raw bytes from the DRMContent stream.
        aes_key: 16-byte AES key from the use license.
        iv: 16-byte initialization vector.

    Returns:
        Decrypted Office document bytes.
    """
    if len(encrypted_content) < 8:
        raise IRMDecryptionError("Encrypted content too short (< 8 bytes)")

    # The first 8 bytes are a header: original_size (uint32) + flags (uint32)
    original_size = struct.unpack_from("<I", encrypted_content, 0)[0]
    ciphertext = encrypted_content[8:]

    if len(ciphertext) == 0:
        raise IRMDecryptionError("No ciphertext after header")

    # AES-CBC requires ciphertext to be a multiple of the block size.
    # A non-aligned length means the data is truncated or corrupt.
    block_size = 16
    if len(ciphertext) % block_size != 0:
        raise IRMDecryptionError(
            f"Ciphertext length ({len(ciphertext)}) is not a multiple of "
            f"the AES block size ({block_size}). The encrypted content "
            "may be truncated or corrupt."
        )

    # Decrypt with AES-128-CBC
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_padding

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()

    # Trim to original size if known
    if 0 < original_size < len(plaintext):
        plaintext = plaintext[:original_size]
    else:
        # Try PKCS7 unpadding as fallback
        try:
            unpadder = sym_padding.PKCS7(128).unpadder()
            plaintext = unpadder.update(plaintext) + unpadder.finalize()
        except ValueError:
            # Not PKCS7 padded — use as-is
            pass

    return plaintext
