"""Ed25519 signatures (RFC 8032), in pure Python.

Homestead checks the signature on its list of validated versions
(homestead_validated) with this, so the image needs no crypto library. The
code follows RFC 8032's reference implementation, section 6. It is not
constant-time: verifying is public work, and signing runs only in CI
(scripts/validated_versions.py), never in Homestead.

tests/test_ed25519.py checks it against the RFC's own test vectors.
"""
import hashlib

P = 2 ** 255 - 19
Q = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
_SQRT_M1 = pow(2, (P - 1) // 4, P)


def _inverse(x):
    return pow(x, P - 2, P)


def _add(a, b):
    """Two points in extended coordinates (X, Y, Z, T)."""
    one = (a[1] - a[0]) * (b[1] - b[0]) % P
    two = (a[1] + a[0]) * (b[1] + b[0]) % P
    three = 2 * a[3] * b[3] * D % P
    four = 2 * a[2] * b[2] % P
    e, f, g, h = two - one, four - three, four + three, two + one
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _multiply(scalar, point):
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def _recover_x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * _inverse(D * y * y + 1) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * _SQRT_M1 % P
    if (x * x - x2) % P:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


_GY = 4 * _inverse(5) % P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % P)


def _compress(point):
    z = _inverse(point[2])
    x, y = point[0] * z % P, point[1] * z % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(data):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def _hash(data):
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % Q


def verify(public, message, signature):
    """Whether `signature` (64 bytes) is `public`'s (32 bytes) on `message`."""
    if len(public) != 32 or len(signature) != 64:
        return False
    point = _decompress(public)
    r = _decompress(signature[:32])
    if point is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= Q:
        return False
    h = _hash(signature[:32] + public + message)
    return _equal(_multiply(s, _G), _add(r, _multiply(h, point)))


def _expand(secret):
    if len(secret) != 32:
        raise ValueError("An Ed25519 private key is 32 bytes.")
    digest = hashlib.sha512(secret).digest()
    a = int.from_bytes(digest[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, digest[32:]


def public_key(secret):
    return _compress(_multiply(_expand(secret)[0], _G))


def sign(secret, message):
    """For CI only: the 64-byte signature of `message` with the 32-byte private key."""
    a, prefix = _expand(secret)
    public = _compress(_multiply(a, _G))
    r = _hash(prefix + message)
    r_point = _compress(_multiply(r, _G))
    s = (r + _hash(r_point + public + message) * a) % Q
    return r_point + int.to_bytes(s, 32, "little")
