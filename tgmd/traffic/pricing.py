"""Prices and display names, read out of mihomo node names.

The proxy service bills per GB for each node and puts the price in the node's
name, for example ``🖤东京京X06｜0.01元/G｜Reality｜``. That is the only price
list there is, so it is parsed rather than configured.
"""

from __future__ import annotations

import re
import unicodedata

# NFKC first, so full-width digits, the full-width full stop and slash all
# read as their ASCII forms.
_PRICE = re.compile(r"(\d+(?:\.\d+)?)\s*元\s*/\s*G", re.IGNORECASE)
_SEPARATORS = re.compile(r"[｜|]")


def parse_price(name: str) -> float | None:
    """CNY per GB written in a node name, or None when it carries none."""
    match = _PRICE.search(unicodedata.normalize("NFKC", name or ""))
    return float(match.group(1)) if match else None


def short_node(name: str) -> str:
    """A node name as a person reads it: no decoration, price kept.

    ``😈英格兰002｜0.09元/G｜hy2｜`` becomes ``英格兰002 0.09元/G``.
    """
    if not name:
        return ""
    parts = [part.strip() for part in _SEPARATORS.split(name) if part.strip()]
    if not parts:
        return name.strip()
    label = parts[0]
    # Leading emoji and symbols are not part of the name.
    while label and not (label[0].isalnum() or label[0] == "_"):
        label = label[1:]
    label = label.strip() or parts[0]
    price = parse_price(name)
    if price is None:
        return label
    return f"{label} {price:g}元/G"
