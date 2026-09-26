"""Validate Kubernetes memory requests and optional per-container limits."""
from decimal import Decimal, InvalidOperation
import re


_SUFFIXES = {"": Decimal(1), "n": Decimal("0.000000001"),
             "u": Decimal("0.000001"), "m": Decimal("0.001")}
for _power, _letter in enumerate("kMGTPE", 1):
    _SUFFIXES[_letter] = Decimal(1000) ** _power
    _SUFFIXES[_letter.upper() + "i"] = Decimal(1024) ** _power
_SUFFIXES["Ki"] = Decimal(1024)
_SUFFIXES["K"] = Decimal(1000)  # Accept the familiar spelling as well as Kubernetes' k.
_QUANTITY = re.compile(r"^(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(Ki|Mi|Gi|Ti|Pi|Ei|[kKMGTPE]|[num])?$")


def bytes_for(value, label="memory"):
    """Return a positive byte quantity, or None for an unset optional field."""
    value = str(value or "").strip()
    if not value:
        return None
    match = _QUANTITY.fullmatch(value)
    if not match:
        raise ValueError(f"{label} must be a positive size such as 128Mi or 2Gi")
    try:
        amount = Decimal(match.group(1)) * _SUFFIXES[match.group(2) or ""]
    except (InvalidOperation, KeyError) as error:
        raise ValueError(f"{label} must be a positive size such as 128Mi or 2Gi") from error
    if amount <= 0 or amount > 2**63 - 1:
        raise ValueError(f"{label} is outside the supported positive memory range")
    return amount


def validate(request, limit, name="container"):
    """Preserve optional limits, but never submit a ceiling below the request."""
    requested = bytes_for(request, f"{name} memory reserved")
    maximum = bytes_for(limit, f"{name} memory max")
    if requested is not None and maximum is not None and maximum < requested:
        raise ValueError(f"{name}: memory max must be at least the memory reserved ({request})")
    return requested, maximum
