"""Twofish 128-bit block cipher.

Public algorithm: Schneier, Kelsey, Whiting, Wagner, Hall, Ferguson.
This module is an independent Python implementation of the cipher. Round
function and q-box construction follow the Twofish paper and the widely
used Gladman reference layout so official known-answer tests pass.

Dr B. R. Gladman's original C implementation (14 Jan 1999) is freely
usable with acknowledgment of origin; that notice is preserved here.
"""

from __future__ import annotations

import struct

BLOCK_SIZE = 16

# q-permutation 4-bit tables from the Twofish paper (q0 = row 0, q1 = row 1).
_QT0 = (
    (8, 1, 7, 13, 6, 15, 3, 2, 0, 11, 5, 9, 14, 12, 10, 4),
    (2, 8, 11, 13, 15, 7, 6, 14, 3, 1, 9, 4, 0, 10, 12, 5),
)
_QT1 = (
    (14, 12, 11, 8, 1, 2, 3, 5, 15, 4, 10, 6, 7, 0, 9, 13),
    (1, 14, 2, 11, 4, 12, 3, 7, 6, 13, 10, 5, 15, 9, 0, 8),
)
_QT2 = (
    (11, 10, 5, 14, 6, 13, 9, 0, 12, 8, 15, 3, 2, 4, 7, 1),
    (4, 12, 7, 5, 1, 6, 9, 10, 0, 14, 13, 8, 2, 11, 3, 15),
)
_QT3 = (
    (13, 7, 15, 4, 1, 2, 6, 14, 9, 11, 3, 0, 8, 5, 12, 10),
    (11, 9, 5, 1, 12, 3, 13, 14, 6, 4, 7, 15, 2, 0, 8, 10),
)
_ROR4 = (0, 8, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15)
_ASHX = (0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12, 5, 14, 7)
_TAB_5B = (0, 90, 180, 238)
_TAB_EF = (0, 238, 180, 90)


def _rotr32(x: int, n: int) -> int:
    return ((x >> n) | ((x << (32 - n)) & 0xFFFFFFFF)) & 0xFFFFFFFF


def _rotl32(x: int, n: int) -> int:
    return (((x << n) & 0xFFFFFFFF) | (x >> (32 - n))) & 0xFFFFFFFF


def _byte(x: int, n: int) -> int:
    return (x >> (8 * n)) & 0xFF


def _qp(n: int, x: int) -> int:
    a0, b0 = x >> 4, x & 15
    a1 = a0 ^ b0
    b1 = _ROR4[b0] ^ _ASHX[a0]
    a2, b2 = _QT0[n][a1], _QT1[n][b1]
    a3 = a2 ^ b2
    b3 = _ROR4[b2] ^ _ASHX[a2]
    a4, b4 = _QT2[n][a3], _QT3[n][b3]
    return (b4 << 4) | a4


def _gen_qtab() -> tuple[list[int], list[int]]:
    return ([_qp(0, i) for i in range(256)], [_qp(1, i) for i in range(256)])


def _mds_mul_terms(f01: int) -> tuple[int, int]:
    f5b = f01 ^ (f01 >> 2) ^ _TAB_5B[f01 & 3]
    fef = f01 ^ (f01 >> 1) ^ (f01 >> 2) ^ _TAB_EF[f01 & 3]
    return f5b, fef


def _gen_mtab(q0: list[int], q1: list[int]) -> list[list[int]]:
    m_tab = [[0] * 256 for _ in range(4)]
    for i in range(256):
        f01 = q1[i]
        f5b, fef = _mds_mul_terms(f01)
        m_tab[0][i] = f01 + (f5b << 8) + (fef << 16) + (fef << 24)
        m_tab[2][i] = f5b + (fef << 8) + (f01 << 16) + (fef << 24)
        f01 = q0[i]
        f5b, fef = _mds_mul_terms(f01)
        m_tab[1][i] = fef + (fef << 8) + (f5b << 16) + (f01 << 24)
        m_tab[3][i] = f5b + (f01 << 8) + (fef << 16) + (f5b << 24)
    return m_tab


def _mds_rem(p0: int, p1: int) -> int:
    for _ in range(8):
        t = p1 >> 24
        p1 = ((p1 << 8) & 0xFFFFFFFF) | (p0 >> 24)
        p0 = (p0 << 8) & 0xFFFFFFFF
        u = (t << 1) & 0xFFFFFFFF
        if t & 0x80:
            u ^= 0x0000014D
        p1 ^= t ^ ((u << 16) & 0xFFFFFFFF)
        u ^= t >> 1
        if t & 0x01:
            u ^= 0x0000014D >> 1
        p1 ^= ((u << 24) & 0xFFFFFFFF) | ((u << 8) & 0xFFFFFFFF)
    return p1


def _h_fun(
    x: int,
    key: list[int],
    k_len: int,
    q_tab: tuple[list[int], list[int]],
    m_tab: list[list[int]],
) -> int:
    q0, q1 = q_tab
    b0, b1, b2, b3 = _byte(x, 0), _byte(x, 1), _byte(x, 2), _byte(x, 3)
    if k_len >= 4:
        b0 = q1[b0] ^ _byte(key[3], 0)
        b1 = q0[b1] ^ _byte(key[3], 1)
        b2 = q0[b2] ^ _byte(key[3], 2)
        b3 = q1[b3] ^ _byte(key[3], 3)
    if k_len >= 3:
        b0 = q1[b0] ^ _byte(key[2], 0)
        b1 = q1[b1] ^ _byte(key[2], 1)
        b2 = q0[b2] ^ _byte(key[2], 2)
        b3 = q0[b3] ^ _byte(key[2], 3)
    b0 = q0[q0[b0] ^ _byte(key[1], 0)] ^ _byte(key[0], 0)
    b1 = q0[q1[b1] ^ _byte(key[1], 1)] ^ _byte(key[0], 1)
    b2 = q1[q0[b2] ^ _byte(key[1], 2)] ^ _byte(key[0], 2)
    b3 = q1[q1[b3] ^ _byte(key[1], 3)] ^ _byte(key[0], 3)
    return m_tab[0][b0] ^ m_tab[1][b1] ^ m_tab[2][b2] ^ m_tab[3][b3]


def _gen_mk_tab(
    k_len: int,
    s_key: list[int],
    q_tab: tuple[list[int], list[int]],
    m_tab: list[list[int]],
) -> list[list[int]]:
    q0, q1 = q_tab
    mk = [[0] * 256 for _ in range(4)]
    if k_len == 2:
        for i in range(256):
            mk[0][i] = m_tab[0][q0[q0[i] ^ _byte(s_key[1], 0)] ^ _byte(s_key[0], 0)]
            mk[1][i] = m_tab[1][q0[q1[i] ^ _byte(s_key[1], 1)] ^ _byte(s_key[0], 1)]
            mk[2][i] = m_tab[2][q1[q0[i] ^ _byte(s_key[1], 2)] ^ _byte(s_key[0], 2)]
            mk[3][i] = m_tab[3][q1[q1[i] ^ _byte(s_key[1], 3)] ^ _byte(s_key[0], 3)]
    elif k_len == 3:
        for i in range(256):
            mk[0][i] = m_tab[0][
                q0[q0[q1[i] ^ _byte(s_key[2], 0)] ^ _byte(s_key[1], 0)] ^ _byte(s_key[0], 0)
            ]
            mk[1][i] = m_tab[1][
                q0[q1[q1[i] ^ _byte(s_key[2], 1)] ^ _byte(s_key[1], 1)] ^ _byte(s_key[0], 1)
            ]
            mk[2][i] = m_tab[2][
                q1[q0[q0[i] ^ _byte(s_key[2], 2)] ^ _byte(s_key[1], 2)] ^ _byte(s_key[0], 2)
            ]
            mk[3][i] = m_tab[3][
                q1[q1[q0[i] ^ _byte(s_key[2], 3)] ^ _byte(s_key[1], 3)] ^ _byte(s_key[0], 3)
            ]
    else:
        for i in range(256):
            mk[0][i] = m_tab[0][
                q0[
                    q0[q1[q1[i] ^ _byte(s_key[3], 0)] ^ _byte(s_key[2], 0)]
                    ^ _byte(s_key[1], 0)
                ]
                ^ _byte(s_key[0], 0)
            ]
            mk[1][i] = m_tab[1][
                q0[
                    q1[q1[q0[i] ^ _byte(s_key[3], 1)] ^ _byte(s_key[2], 1)]
                    ^ _byte(s_key[1], 1)
                ]
                ^ _byte(s_key[0], 1)
            ]
            mk[2][i] = m_tab[2][
                q1[
                    q0[q0[q0[i] ^ _byte(s_key[3], 2)] ^ _byte(s_key[2], 2)]
                    ^ _byte(s_key[1], 2)
                ]
                ^ _byte(s_key[0], 2)
            ]
            mk[3][i] = m_tab[3][
                q1[
                    q1[q0[q1[i] ^ _byte(s_key[3], 3)] ^ _byte(s_key[2], 3)]
                    ^ _byte(s_key[1], 3)
                ]
                ^ _byte(s_key[0], 3)
            ]
    return mk


class Twofish:
    """Twofish with 128/192/256-bit keys. Packet Tracer uses a 128-bit key."""

    block_size = BLOCK_SIZE

    def __init__(self, key: bytes) -> None:
        if len(key) not in (16, 24, 32):
            raise ValueError("Twofish key must be 16, 24, or 32 bytes")
        q_tab = _gen_qtab()
        m_tab = _gen_mtab(q_tab[0], q_tab[1])
        k_len = (len(key) * 8) // 64
        words = list(struct.unpack(f"<{len(key) // 4}I", key)) + [0] * 8
        me_key = [0, 0, 0, 0]
        mo_key = [0, 0, 0, 0]
        s_key = [0, 0, 0, 0]
        for i in range(k_len):
            a = words[i + i]
            b = words[i + i + 1]
            me_key[i] = a
            mo_key[i] = b
            s_key[k_len - i - 1] = _mds_rem(a, b)
        l_key = [0] * 40
        for i in range(0, 40, 2):
            a = (0x01010101 * i) & 0xFFFFFFFF
            b = (a + 0x01010101) & 0xFFFFFFFF
            a = _h_fun(a, me_key, k_len, q_tab, m_tab)
            b = _rotl32(_h_fun(b, mo_key, k_len, q_tab, m_tab), 8)
            l_key[i] = (a + b) & 0xFFFFFFFF
            l_key[i + 1] = _rotl32((a + 2 * b) & 0xFFFFFFFF, 9)
        self._k_len = k_len
        self._l_key = l_key
        self._mk_tab = _gen_mk_tab(k_len, s_key, q_tab, m_tab)

    def _g(self, x: int, rotate: bool) -> int:
        mk = self._mk_tab
        if rotate:
            return (
                mk[0][_byte(x, 3)]
                ^ mk[1][_byte(x, 0)]
                ^ mk[2][_byte(x, 1)]
                ^ mk[3][_byte(x, 2)]
            )
        return (
            mk[0][_byte(x, 0)]
            ^ mk[1][_byte(x, 1)]
            ^ mk[2][_byte(x, 2)]
            ^ mk[3][_byte(x, 3)]
        )

    def encrypt(self, block: bytes) -> bytes:
        if len(block) != BLOCK_SIZE:
            raise ValueError("Twofish block must be exactly 16 bytes")
        k = self._l_key
        a, b, c, d = struct.unpack("<4I", block)
        a ^= k[0]
        b ^= k[1]
        c ^= k[2]
        d ^= k[3]
        for i in range(8):
            t0 = self._g(a, False)
            t1 = self._g(b, True)
            c = _rotr32(c ^ ((t0 + t1 + k[4 * i + 8]) & 0xFFFFFFFF), 1)
            d = _rotl32(d, 1) ^ ((t0 + 2 * t1 + k[4 * i + 9]) & 0xFFFFFFFF)
            t0 = self._g(c, False)
            t1 = self._g(d, True)
            a = _rotr32(a ^ ((t0 + t1 + k[4 * i + 10]) & 0xFFFFFFFF), 1)
            b = _rotl32(b, 1) ^ ((t0 + 2 * t1 + k[4 * i + 11]) & 0xFFFFFFFF)
        return struct.pack("<4I", c ^ k[4], d ^ k[5], a ^ k[6], b ^ k[7])

    def decrypt(self, block: bytes) -> bytes:
        if len(block) != BLOCK_SIZE:
            raise ValueError("Twofish block must be exactly 16 bytes")
        k = self._l_key
        a, b, c, d = struct.unpack("<4I", block)
        a ^= k[4]
        b ^= k[5]
        c ^= k[6]
        d ^= k[7]
        for i in range(7, -1, -1):
            t0 = self._g(a, False)
            t1 = self._g(b, True)
            c = _rotl32(c, 1) ^ ((t0 + t1 + k[4 * i + 10]) & 0xFFFFFFFF)
            d = _rotr32(d ^ ((t0 + 2 * t1 + k[4 * i + 11]) & 0xFFFFFFFF), 1)
            t0 = self._g(c, False)
            t1 = self._g(d, True)
            a = _rotl32(a, 1) ^ ((t0 + t1 + k[4 * i + 8]) & 0xFFFFFFFF)
            b = _rotr32(b ^ ((t0 + 2 * t1 + k[4 * i + 9]) & 0xFFFFFFFF), 1)
        return struct.pack("<4I", c ^ k[0], d ^ k[1], a ^ k[2], b ^ k[3])
