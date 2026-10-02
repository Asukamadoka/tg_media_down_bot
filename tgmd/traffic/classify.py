"""Putting one mihomo connection into an outbound and a category.

Pure functions over the JSON mihomo returns, so every rule is testable
without a proxy.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from .pricing import parse_price

CATEGORIES = ("probe", "telegram", "pikpak", "model", "lan", "proxy-sub", "other")
PROBE_LISTENER = "probe"
"""``metadata.inboundName`` of the listener the speed probe goes through."""

# Telegram's published address ranges.
TELEGRAM_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "91.105.192.0/23",
        "91.108.4.0/22",
        "91.108.8.0/22",
        "91.108.12.0/22",
        "91.108.16.0/22",
        "91.108.20.0/22",
        "91.108.56.0/22",
        "95.161.64.0/20",
        "149.154.160.0/20",
        "185.76.151.0/24",
        "2001:67c:4e8::/48",
        "2001:b28:f23c::/46",
    )
)
TELEGRAM_DOMAINS = (
    "telegram.org",
    "telegram.me",
    "telegram.dog",
    "t.me",
    "telegra.ph",
    "telesco.pe",
    "tdesktop.com",
    "telegram-cdn.org",
    "cdn-telegram.org",
)
PIKPAK_DOMAINS = ("mypikpak.com", "mypikpak.net")
MODEL_DOMAINS = ("ollama.com", "ollama.ai", "r2.cloudflarestorage.com")
SUBSCRIPTION_DOMAINS = ("bujidao.cc",)
MODEL_HOST = ("<LAN_IP>", 11434)

_TG_GROUP = re.compile(r"^TG(-.*)?$", re.IGNORECASE)


@dataclass(frozen=True)
class Classified:
    outbound: str
    """``direct`` or ``proxy``."""
    node: str
    """The proxy node's own name; empty for direct traffic."""
    group: str
    category: str
    host: str
    destination: str
    price: float | None
    leak: bool
    """A connection that should have gone direct but went through the proxy."""


def _in_domains(host: str, domains: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _ip(value: str):
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def split_chains(chains: list[str]) -> tuple[str, str]:
    """``(node, group)`` of a proxied connection.

    The node is the element that carries a price in its name, which does not
    depend on which end of the list mihomo puts it. Without one the first
    element is the group and the last is the node (docs/wms/M9 §A.2).
    """
    priced = [name for name in chains if parse_price(name) is not None]
    if priced:
        node = priced[-1]
        group = next((name for name in chains if name != node), "")
        return node, group
    return chains[-1], chains[0]


def classify(connection: dict) -> Classified:
    """Outbound, category and the facts alerts need, for one connection."""
    metadata = connection.get("metadata") or {}
    chains = [str(name) for name in (connection.get("chains") or [])]
    host = str(metadata.get("host") or "").strip().lower()
    dest_ip = str(metadata.get("destinationIP") or "").strip()
    dest_port = str(metadata.get("destinationPort") or "").strip()
    address = _ip(dest_ip)
    if not host:
        host = dest_ip

    direct = not chains or any(name.upper() == "DIRECT" for name in chains)
    if direct:
        node, group = "", (chains[0] if chains else "DIRECT")
    else:
        node, group = split_chains(chains)

    on_tg_group = any(_TG_GROUP.match(name) for name in chains)
    is_mac = dest_ip == MODEL_HOST[0] and (not dest_port or dest_port == str(MODEL_HOST[1]))

    if str(metadata.get("inboundName") or "") == PROBE_LISTENER:
        category = "probe"
    elif (
        on_tg_group
        or _in_domains(host, TELEGRAM_DOMAINS)
        or (address is not None and any(address in net for net in TELEGRAM_NETWORKS
                                        if net.version == address.version))
    ):
        category = "telegram"
    elif _in_domains(host, PIKPAK_DOMAINS):
        category = "pikpak"
    elif _in_domains(host, MODEL_DOMAINS) or is_mac:
        category = "model"
    elif address is not None and (address.is_private or address.is_loopback):
        category = "lan"
    elif _in_domains(host, SUBSCRIPTION_DOMAINS):
        category = "proxy-sub"
    else:
        category = "other"

    outbound = "direct" if direct else "proxy"
    # Ollama pulls through the proxy are expected (and billed): only the Mac
    # model host going through it means the rules are wrong.
    leak = outbound == "proxy" and (
        category in ("pikpak", "lan") or (category == "model" and is_mac)
    )
    return Classified(
        outbound=outbound,
        node=node,
        group=group,
        category=category,
        host=host,
        destination=f"{dest_ip}:{dest_port}" if dest_port else dest_ip,
        price=parse_price(node) if node else None,
        leak=leak,
    )
