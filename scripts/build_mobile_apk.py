from __future__ import annotations

import argparse
import base64
import hashlib
import struct
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APK_MAGIC = b"APK Sig Block 42"
V2_BLOCK_ID = 0x7109871A
RSA_PKCS1_SHA256 = 0x0103
CHUNK_SIZE = 1024 * 1024


def lp(value: bytes) -> bytes:
    return struct.pack("<I", len(value)) + value


def digest(value: bytes) -> str:
    return base64.b64encode(hashlib.sha256(value).digest()).decode("ascii")


def manifest_files(entries: list[tuple[str, bytes, int]]) -> tuple[bytes, bytes]:
    sections: list[bytes] = []
    for name, data, _compression in entries:
        section = (
            f"Name: {name}\r\nSHA-256-Digest: {digest(data)}\r\n\r\n"
        ).encode("utf-8")
        sections.append(section)
    manifest = b"Manifest-Version: 1.0\r\nCreated-By: EIRVEN r37\r\n\r\n" + b"".join(sections)
    sf_sections = []
    for (name, _data, _compression), section in zip(entries, sections, strict=True):
        sf_sections.append(
            f"Name: {name}\r\nSHA-256-Digest: {digest(section)}\r\n\r\n".encode("utf-8")
        )
    signature_file = (
        b"Signature-Version: 1.0\r\n"
        b"Created-By: EIRVEN r37\r\n"
        b"X-Android-APK-Signed: 2\r\n"
        + f"SHA-256-Digest-Manifest: {digest(manifest)}\r\n\r\n".encode("ascii")
        + b"".join(sf_sections)
    )
    return manifest, signature_file


def signing_identity():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover - developer-only build dependency
        raise RuntimeError("Install cryptography to build the mobile APK") from exc
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "EIRVEN Mobile r37"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "EIRVEN"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def pkcs7_signature(data: bytes, key, cert) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs7

    return (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(data)
        .add_signer(cert, key, hashes.SHA256())
        .sign(
            serialization.Encoding.DER,
            [
                pkcs7.PKCS7Options.DetachedSignature,
                pkcs7.PKCS7Options.Binary,
                # Android's legacy verifier signs CERT.SF bytes directly.  Omitting
                # authenticated CMS attributes keeps the fallback v1 lane compatible
                # with API 21-era PackageManager implementations.
                pkcs7.PKCS7Options.NoAttributes,
            ],
        )
    )


def _aligned_extra(offset: int, name: str) -> bytes:
    # A valid private ZIP extra field aligns every stored entry on four bytes.
    base = offset + 30 + len(name.encode("utf-8"))
    padding = (-base - 4) % 4
    return struct.pack("<HH", 0xD935, padding) + (b"\0" * padding)


def write_v1_apk(
    target: Path,
    entries: list[tuple[str, bytes, int]],
    signature_file: bytes,
    signature_block: bytes,
) -> None:
    signed = [
        ("META-INF/MANIFEST.MF", manifest_files(entries)[0], zipfile.ZIP_DEFLATED),
        ("META-INF/EIRVEN37.SF", signature_file, zipfile.ZIP_DEFLATED),
        ("META-INF/EIRVEN37.RSA", signature_block, zipfile.ZIP_DEFLATED),
        *entries,
    ]
    with zipfile.ZipFile(target, "w", allowZip64=False) as archive:
        for name, data, compression in signed:
            info = zipfile.ZipInfo(name, date_time=(2026, 8, 12, 12, 0, 0))
            info.compress_type = compression
            info.flag_bits = 0x800
            info.create_system = 0
            info.external_attr = 0
            if compression == zipfile.ZIP_STORED:
                info.extra = _aligned_extra(archive.fp.tell(), name)
            archive.writestr(info, data, compress_type=compression, compresslevel=7)


def _eocd(data: bytes) -> tuple[int, int]:
    offset = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if offset < 0:
        raise RuntimeError("APK has no ZIP end record")
    central_offset = struct.unpack_from("<I", data, offset + 16)[0]
    return offset, central_offset


def apk_content_digest(data: bytes) -> bytes:
    eocd_offset, central_offset = _eocd(data)
    eocd = bytearray(data[eocd_offset:])
    # During verification this points to the start of the APK Signing Block, not
    # to the central directory after that block has been inserted.
    struct.pack_into("<I", eocd, 16, central_offset)
    sections = (data[:central_offset], data[central_offset:eocd_offset], bytes(eocd))
    chunks: list[bytes] = []
    for section in sections:
        for start in range(0, len(section), CHUNK_SIZE):
            chunk = section[start : start + CHUNK_SIZE]
            chunks.append(hashlib.sha256(b"\xA5" + struct.pack("<I", len(chunk)) + chunk).digest())
    return hashlib.sha256(b"\x5A" + struct.pack("<I", len(chunks)) + b"".join(chunks)).digest()


def add_v2_signature(source: Path, target: Path, key, cert) -> None:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    data = source.read_bytes()
    eocd_offset, central_offset = _eocd(data)
    content_digest = apk_content_digest(data)
    digest_record = struct.pack("<I", RSA_PKCS1_SHA256) + lp(content_digest)
    certificates = lp(cert.public_bytes(serialization.Encoding.DER))
    signed_data = lp(lp(digest_record)) + lp(certificates) + lp(b"")
    signature = key.sign(signed_data, padding.PKCS1v15(), hashes.SHA256())
    signature_record = struct.pack("<I", RSA_PKCS1_SHA256) + lp(signature)
    public_key = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signer = lp(signed_data) + lp(lp(signature_record)) + lp(public_key)
    # The scheme block starts with one length-prefixed "signers" field, whose
    # contents are themselves a sequence of length-prefixed signer records.
    v2_value = lp(lp(signer))
    pair_value = struct.pack("<I", V2_BLOCK_ID) + v2_value
    pairs = struct.pack("<Q", len(pair_value)) + pair_value
    block_size = len(pairs) + 8 + len(APK_MAGIC)
    signing_block = struct.pack("<Q", block_size) + pairs + struct.pack("<Q", block_size) + APK_MAGIC
    central = data[central_offset:eocd_offset]
    eocd = bytearray(data[eocd_offset:])
    struct.pack_into("<I", eocd, 16, central_offset + len(signing_block))
    target.write_bytes(data[:central_offset] + signing_block + central + bytes(eocd))


def load_entries(base_apk: Path, assets_dir: Path) -> list[tuple[str, bytes, int]]:
    result: list[tuple[str, bytes, int]] = []
    with zipfile.ZipFile(base_apk) as archive:
        for item in archive.infolist():
            name = item.filename
            if name.startswith("META-INF/"):
                continue
            data = archive.read(name)
            local_asset = assets_dir / Path(name).name if name.startswith("assets/") else None
            if local_asset is not None and local_asset.is_file():
                data = local_asset.read_bytes()
            if name == "AndroidManifest.xml":
                old_version = "1.9.5".encode("utf-16le")
                new_version = "1.9.6".encode("utf-16le")
                if old_version in data:
                    data = data.replace(old_version, new_version, 1)
                    data = data.replace(struct.pack("<I", 10905), struct.pack("<I", 10906), 1)
                elif new_version not in data:
                    raise RuntimeError("Base manifest is not the expected 1.9.5/1.9.6 build")
            compression = (
                zipfile.ZIP_STORED
                if item.compress_type == zipfile.ZIP_STORED or name == "resources.arsc"
                else zipfile.ZIP_DEFLATED
            )
            result.append((name, data, compression))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a v1+v2 signed EIRVEN Mobile APK")
    parser.add_argument("--base", type=Path, default=ROOT / "mobile_client" / "EIRVEN-Mobile.apk")
    parser.add_argument("--output", type=Path, default=ROOT / "mobile_client" / "EIRVEN-Mobile-r37.apk")
    args = parser.parse_args()
    entries = load_entries(args.base.resolve(), ROOT / "mobile_client" / "assets")
    manifest, signature_file = manifest_files(entries)
    key, cert = signing_identity()
    rsa_block = pkcs7_signature(signature_file, key, cert)
    v1_path = args.output.with_suffix(".v1.apk")
    write_v1_apk(v1_path, entries, signature_file, rsa_block)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    add_v2_signature(v1_path, args.output, key, cert)
    v1_path.unlink(missing_ok=True)
    print(args.output.resolve())
    print(hashlib.sha256(args.output.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
