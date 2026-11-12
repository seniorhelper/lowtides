"""Ed25519 signatures (RFC 8032), in pure Python. Standard library only.

Used to sign Lowtides Passes. Correctness is checked against the RFC 8032 test
vectors in tests/test_pass.py. Python big integers are not constant time, so this is
fine for signing on your own server, where an attacker can't time the private-key
math precisely, but don't reuse it where they can.
"""
import hashlib
import os

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493  # group order
D = (-121665 * pow(121666, P - 2, P)) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _sha512(b):
    return hashlib.sha512(b).digest()


def _inv(x):
    return pow(x, P - 2, P)


# Points in extended coordinates (X, Y, Z, T) with x = X/Z, y = Y/Z, x*y = T/Z.
def _add(p, q):
    """Complete twisted Edwards addition (RFC 8032, 5.1.4); also used for doubling."""
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = 2 * t1 * t2 * D % P
    d = 2 * z1 * z2 % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


IDENTITY = (0, 1, 1, 0)


def _mul(s, p):
    q = IDENTITY
    while s > 0:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _equal(p, q):
    # x1/z1 == x2/z2 and y1/z1 == y2/z2
    return (p[0] * q[2] - q[0] * p[2]) % P == 0 and (p[1] * q[2] - q[1] * p[2]) % P == 0


def _recover_x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * _inv(D * y * y + 1) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


_GY = 4 * _inv(5) % P
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % P)


def _encode(p):
    zi = _inv(p[2])
    x, y = p[0] * zi % P, p[1] * zi % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decode(b):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def _expand(seed):
    if len(seed) != 32:
        raise ValueError("An Ed25519 private key is 32 bytes.")
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed):
    """32-byte public key for a 32-byte private key (seed)."""
    a, _ = _expand(seed)
    return _encode(_mul(a, G))


def sign(seed, msg):
    a, prefix = _expand(seed)
    pub = _encode(_mul(a, G))
    r = int.from_bytes(_sha512(prefix + msg), "little") % L
    big_r = _encode(_mul(r, G))
    k = int.from_bytes(_sha512(big_r + pub + msg), "little") % L
    s = (r + k * a) % L
    return big_r + int.to_bytes(s, 32, "little")


def verify(pub, msg, sig):
    """True only for a valid signature. Never raises on bad input."""
    try:
        if len(pub) != 32 or len(sig) != 64:
            return False
        a_pt = _decode(bytes(pub))
        r_pt = _decode(bytes(sig[:32]))
        if a_pt is None or r_pt is None:
            return False
        s = int.from_bytes(sig[32:], "little")
        if s >= L:
            return False
        k = int.from_bytes(_sha512(bytes(sig[:32]) + bytes(pub) + bytes(msg)), "little") % L
        return _equal(_mul(s, G), _add(r_pt, _mul(k, a_pt)))
    except (TypeError, ValueError):
        return False


def new_seed():
    return os.urandom(32)
