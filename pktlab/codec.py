"""Packet Tracer .pkt/.pka container codec.

A modern (PT 6.0+) Packet Tracer file is XML put through four reversible
stages. The pipeline was originally documented by Mirco De Zorzi
(mircodz/pka2xml) and reimplemented in Python by jeamxn/cisco-pka-to-xml
and Punkcake21/Unpacket. This module is a clean-room implementation of
that documented pipeline:

1. zlib/qCompress with a 4-byte big-endian uncompressed-length prefix
2. Position-keyed XOR: b[i] ^= (length - i) & 0xff
3. Twofish-EAX, key = 0x89 * 16, IV/nonce = 0x10 * 16, 16-byte tag appended
4. Reverse + position-keyed XOR: out[n-1-i] = in[i] ^ ((n - i*n) & 0xff)

PT 5.x files used only stages 1-2 (XOR + zlib) and are rejected here.
The XML schema is independent of this container; see docs/FORMAT.md.
"""

from __future__ import annotations

import zlib
from pathlib import Path

from .twofish import BLOCK_SIZE, Twofish

PKA_KEY = bytes([0x89] * 16)
PKA_IV = bytes([0x10] * 16)
# Alternate keys used by Packet Tracer "nets" and log files (not .pkt/.pka).
NETS_KEY = bytes([0xBA] * 16)
NETS_IV = bytes([0xBE] * 16)

TARGET_VERSION = "9.0.0.0810"


class PktError(ValueError):
    """Raised when a .pkt/.pka blob cannot be decoded."""


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b, strict=True))


def _gf_double(block: bytes) -> bytes:
    n = (int.from_bytes(block, "big") << 1) & ((1 << 128) - 1)
    if block[0] & 0x80:
        n ^= 0x87
    return n.to_bytes(16, "big")


def _omac(cipher: Twofish, tag: int, msg: bytes) -> bytes:
    """OMAC1 / CMAC of a 16-byte typed prefix concatenated with ``msg``."""
    L = cipher.encrypt(b"\x00" * BLOCK_SIZE)
    k1 = _gf_double(L)
    k2 = _gf_double(k1)
    data = tag.to_bytes(BLOCK_SIZE, "big") + msg
    if len(data) % BLOCK_SIZE == 0:
        last = _xor(data[-BLOCK_SIZE:], k1)
        body = data[:-BLOCK_SIZE]
    else:
        pad_len = BLOCK_SIZE - (len(data) % BLOCK_SIZE)
        padded = data + b"\x80" + b"\x00" * (pad_len - 1)
        last = _xor(padded[-BLOCK_SIZE:], k2)
        body = padded[:-BLOCK_SIZE]
    state = b"\x00" * BLOCK_SIZE
    for i in range(0, len(body), BLOCK_SIZE):
        state = cipher.encrypt(_xor(state, body[i : i + BLOCK_SIZE]))
    return cipher.encrypt(_xor(state, last))


def _ctr(cipher: Twofish, nonce_block: bytes, data: bytes) -> bytes:
    out = bytearray()
    base = int.from_bytes(nonce_block, "big")
    mask = (1 << 128) - 1
    for i in range(0, len(data), BLOCK_SIZE):
        ks = cipher.encrypt(((base + i // BLOCK_SIZE) & mask).to_bytes(BLOCK_SIZE, "big"))
        chunk = data[i : i + BLOCK_SIZE]
        out.extend(x ^ y for x, y in zip(chunk, ks[: len(chunk)], strict=True))
    return bytes(out)


def eax_encrypt(cipher: Twofish, nonce: bytes, plaintext: bytes, header: bytes = b"") -> bytes:
    n = _omac(cipher, 0, nonce)
    h = _omac(cipher, 1, header)
    ct = _ctr(cipher, n, plaintext)
    c = _omac(cipher, 2, ct)
    tag = bytes(x ^ y ^ z for x, y, z in zip(n, h, c, strict=True))
    return ct + tag


def eax_decrypt(cipher: Twofish, nonce: bytes, ct_with_tag: bytes, header: bytes = b"") -> bytes:
    if len(ct_with_tag) < BLOCK_SIZE:
        raise PktError("ciphertext shorter than EAX tag")
    tag, ct = ct_with_tag[-BLOCK_SIZE:], ct_with_tag[:-BLOCK_SIZE]
    n = _omac(cipher, 0, nonce)
    h = _omac(cipher, 1, header)
    c = _omac(cipher, 2, ct)
    expected = bytes(x ^ y ^ z for x, y, z in zip(n, h, c, strict=True))
    if expected != tag:
        raise PktError("EAX authentication tag mismatch — not a PT 6+ file, or corrupt")
    return _ctr(cipher, n, ct)


def _deobfuscate_stage1(data: bytes) -> bytes:
    n = len(data)
    return bytes((data[n + ~i] ^ ((n - i * n) & 0xFF)) & 0xFF for i in range(n))


def _obfuscate_stage1(data: bytes) -> bytes:
    n = len(data)
    out = bytearray(n)
    for i in range(n):
        out[n + ~i] = data[i] ^ ((n - i * n) & 0xFF)
    return bytes(out)


def _xor_stage2(data: bytes) -> bytes:
    m = len(data)
    return bytes(data[i] ^ ((m - i) & 0xFF) for i in range(m))


def decrypt_pka(data: bytes) -> bytes:
    """Decrypt a .pkt/.pka blob to UTF-8 XML bytes."""
    if len(data) < 20:
        raise PktError("file too small to be a Packet Tracer container")
    stage1 = _deobfuscate_stage1(data)
    cipher = Twofish(PKA_KEY)
    stage2 = eax_decrypt(cipher, PKA_IV, stage1)
    stage3 = _xor_stage2(stage2)
    if len(stage3) < 4:
        raise PktError("decrypted payload too small")
    expected = int.from_bytes(stage3[:4], "big")
    try:
        plain = zlib.decompress(stage3[4:])
    except zlib.error as exc:
        raise PktError(f"zlib decompression failed: {exc}") from exc
    if expected and len(plain) != expected:
        raise PktError(f"declared size {expected} != actual {len(plain)}")
    return plain


def encrypt_pka(xml: bytes) -> bytes:
    """Encrypt UTF-8 XML bytes into a .pkt/.pka blob."""
    compressed = zlib.compress(xml, level=-1)
    stage3 = len(xml).to_bytes(4, "big") + compressed
    stage2 = _xor_stage2(stage3)
    cipher = Twofish(PKA_KEY)
    stage1 = eax_encrypt(cipher, PKA_IV, stage2)
    return _obfuscate_stage1(stage1)


def read_pt_file(path: str | Path) -> bytes:
    return decrypt_pka(Path(path).read_bytes())


def write_pt_file(path: str | Path, xml: bytes) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(encrypt_pka(xml))
    return dest
