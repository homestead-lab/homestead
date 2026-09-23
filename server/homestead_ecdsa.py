"""ECDSA on NIST P-256 with SHA-256, for signing Web Push (VAPID) requests.

Homestead runs on the standard library alone, and a push service only asks it
to sign a short token proving which server sends the pushes. That is ECDSA over
P-256 (ES256): elliptic-curve arithmetic and SHA-256, both small enough to keep
here. Nonces are derived per RFC 6979, so a signature never depends on the
quality of a random number at signing time, and the published RFC 6979 test
vectors check this code exactly.

This module signs. It does not encrypt: pushes carry no payload, and the app
fetches what a push is about over its own HTTPS connection.
"""
import hashlib
import hmac
import secrets

# NIST P-256 (secp256r1)
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _inverse(value, modulus):
    return pow(value, -1, modulus)


# Jacobian coordinates keep the scalar multiplication free of per-step inverses.
def _double(point):
    x, y, z = point
    if not y:
        return (0, 1, 0)
    ysq = y * y % P
    s = 4 * x * ysq % P
    m = (3 * x * x + A * pow(z, 4, P)) % P
    nx = (m * m - 2 * s) % P
    ny = (m * (s - nx) - 8 * ysq * ysq) % P
    nz = 2 * y * z % P
    return (nx, ny, nz)


def _add(p1, p2):
    if not p1[2]:
        return p2
    if not p2[2]:
        return p1
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    z1z1, z2z2 = z1 * z1 % P, z2 * z2 % P
    u1, u2 = x1 * z2z2 % P, x2 * z1z1 % P
    s1, s2 = y1 * z2 * z2z2 % P, y2 * z1 * z1z1 % P
    if u1 == u2:
        return _double(p1) if s1 == s2 else (0, 1, 0)
    h, r = (u2 - u1) % P, (s2 - s1) % P
    h2 = h * h % P
    h3 = h * h2 % P
    u1h2 = u1 * h2 % P
    nx = (r * r - h3 - 2 * u1h2) % P
    ny = (r * (u1h2 - nx) - s1 * h3) % P
    nz = h * z1 * z2 % P
    return (nx, ny, nz)


def _affine(point):
    x, y, z = point
    if not z:
        raise ValueError("the point at infinity has no coordinates")
    zinv = _inverse(z, P)
    return (x * zinv * zinv % P, y * zinv * zinv * zinv % P)


def multiply(k, point=G):
    """k·point, by double-and-add over the scalar's bits."""
    result, addend = (0, 1, 0), (point[0], point[1], 1)
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _double(addend)
        k >>= 1
    return _affine(result)


def on_curve(point):
    x, y = point
    return (y * y - (x * x * x + A * x + B)) % P == 0


def new_private_key():
    return secrets.randbelow(N - 1) + 1


def public_key(private):
    return multiply(private)


def public_bytes(point):
    """Uncompressed SEC1: 0x04 || X || Y, as the Push API's applicationServerKey wants."""
    return b"\x04" + point[0].to_bytes(32, "big") + point[1].to_bytes(32, "big")


def _bits2int(data):
    value = int.from_bytes(data, "big")
    extra = len(data) * 8 - 256
    return value >> extra if extra > 0 else value


def _rfc6979_nonces(private, digest):
    """RFC 6979 §3.2: nonces from the key and the message, via HMAC-SHA256."""
    x = private.to_bytes(32, "big")
    h1 = (_bits2int(digest) % N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = _bits2int(v)
        if 1 <= candidate < N:
            yield candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign(private, message):
    """(r, s) for SHA-256(message), with a deterministic nonce."""
    digest = hashlib.sha256(message).digest()
    e = _bits2int(digest) % N
    for k in _rfc6979_nonces(private, digest):
        r = multiply(k)[0] % N
        if not r:
            continue
        s = _inverse(k, N) * (e + r * private) % N
        if s:
            return r, s
    raise AssertionError("unreachable")


def verify(point, message, signature):
    r, s = signature
    if not (1 <= r < N and 1 <= s < N) or not on_curve(point):
        return False
    e = _bits2int(hashlib.sha256(message).digest()) % N
    w = _inverse(s, N)
    u1, u2 = e * w % N, r * w % N
    total = _add(_to_jacobian(multiply(u1)), _to_jacobian(multiply(u2, point)))
    if not total[2]:
        return False
    return _affine(total)[0] % N == r


def _to_jacobian(point):
    return (point[0], point[1], 1)


def signature_bytes(signature):
    """JWS ES256 wants r || s, 32 bytes each, not DER."""
    return signature[0].to_bytes(32, "big") + signature[1].to_bytes(32, "big")
