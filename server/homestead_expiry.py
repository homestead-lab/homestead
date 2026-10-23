"""Certificates and keys that expire: what, and when, warned some days ahead.

Read only. Python's standard library has no certificate parser, so validity()
walks the DER itself, just far enough for the two validity dates. It reads
cluster input, so every length is checked against what is there and anything
malformed is a ValueError.
"""
import base64
import calendar
import time

DAY = 86400

PEM_BEGIN, PEM_END = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"


def _tlv(buf, i, end):
    """One DER element at i, inside buf[:end]: (tag, start of value, end of value)."""
    if i + 2 > end:
        raise ValueError("certificate ends early")
    tag, length = buf[i], buf[i + 1]
    i += 2
    if length & 0x80:
        n = length & 0x7F
        if not 1 <= n <= 4 or i + n > end:
            raise ValueError("certificate has a bad length")
        length = int.from_bytes(buf[i:i + n], "big")
        i += n
    if i + length > end:
        raise ValueError("certificate ends early")
    return tag, i, i + length


def _children(buf, start, end):
    out = []
    while start < end:
        tag, vs, ve = _tlv(buf, start, end)
        out.append((tag, vs, ve))
        start = ve
    return out


def _time(tag, raw):
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("certificate has a bad date") from None
    if tag == 0x17 and len(text) == 13:          # UTCTime YYMMDDHHMMSSZ
        if not text[:2].isdigit():
            raise ValueError("certificate has a bad date")
        year = int(text[:2])
        text = f"{1900 + year if year >= 50 else 2000 + year}{text[2:]}"
    elif not (tag == 0x18 and len(text) == 15):  # GeneralizedTime YYYYMMDDHHMMSSZ
        raise ValueError("certificate has a bad date")
    if not (text.endswith("Z") and text[:14].isdigit()):
        raise ValueError("certificate has a bad date")
    try:
        parsed = time.strptime(text[:14], "%Y%m%d%H%M%S")
    except ValueError:
        raise ValueError("certificate has a bad date") from None
    return float(calendar.timegm(parsed))


def validity(der):
    """{not_before, not_after} of one DER certificate, as epoch seconds."""
    _, cs, ce = _tlv(der, 0, len(der))
    parts = _children(der, cs, ce)
    if not parts or parts[0][0] != 0x30:
        raise ValueError("not a certificate")
    fields = _children(der, parts[0][1], parts[0][2])
    if fields and fields[0][0] == 0xA0:          # explicit version
        fields = fields[1:]
    # serial, signature, issuer, validity, subject, ...
    if len(fields) < 5 or fields[3][0] != 0x30:
        raise ValueError("not a certificate")
    dates = _children(der, fields[3][1], fields[3][2])
    if len(dates) != 2:
        raise ValueError("not a certificate")
    (tb, bs, be), (ta, as_, ae) = dates
    return {"not_before": _time(tb, der[bs:be]), "not_after": _time(ta, der[as_:ae])}


def pem_certs(text):
    """The DER of every certificate in a PEM bundle, leaf first."""
    out = []
    for chunk in text.split(PEM_BEGIN)[1:]:
        if PEM_END not in chunk:
            break
        body = "".join(chunk.split(PEM_END, 1)[0].split())
        try:
            out.append(base64.b64decode(body, validate=True))
        except ValueError:
            raise ValueError("certificate is not valid base64") from None
    if not out:
        raise ValueError("no certificate")
    return out
