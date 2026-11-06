"""Is this a subscription worth switching to? (docs/wms/M9.6 §C, §D)

Nothing here changes production. :func:`check_url` is the URL hygiene and SSRF guard,
:func:`validate_content` parses and checks a downloaded body, and :func:`validate_url`
chains them around an injected fetch. A refusal is a :class:`Rejected` with a stable
``code``; neither its code nor its text ever contains the URL.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
import socket
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

from .detect import is_real_node
from .fetch import Fetched, Fetcher, FetchError

MAX_URL = 2048
MAX_NODES = 500
ALLOWED_TYPES = frozenset({
    "ss", "vmess", "vless", "trojan", "hysteria", "hysteria2", "tuic", "anytls"})
FORBIDDEN_TYPES = frozenset({"direct", "reject", "reject-drop", "pass", "dns", "compatible"})
URI_SCHEMES = ("vless", "hysteria2", "trojan", "ss", "vmess")

Resolver = Callable[[str], Sequence[str]]
"""Host name to the addresses it resolves to."""
IsDirect = Callable[[str], "bool | None"]
"""Does mihomo route this host DIRECT? None: no evidence either way."""


class Rejected(Exception):
    """Refused. ``code`` is stable (tests and the UI key on it); the text is safe to show."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def system_resolve(host: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except OSError:
        return []


def _private(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return True
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified)


def check_url(url: str, *, allow_private: bool = False,
              resolve: Resolver = system_resolve) -> str:
    """The URL's host, or :class:`Rejected`. ``http``/``https`` only, at most 2048 characters,
    no user info, and no host that resolves to a private or loopback address (the URL
    comes from a chat)."""
    if len(url) > MAX_URL:
        raise Rejected("url_long")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        _ = parts.port
    except ValueError:
        raise Rejected("url_invalid") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise Rejected("url_scheme")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise Rejected("url_userinfo")
    if not host:
        raise Rejected("url_invalid")
    if not allow_private:
        addresses = [host] if _is_ip(host) else list(resolve(host))
        if not addresses:
            raise Rejected("url_unresolved")
        if any(_private(a) for a in addresses):
            raise Rejected("url_private")
    return host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------- parsing


def _port(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _b64(text: str) -> str:
    text = text.strip().replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    return base64.b64decode(text, validate=True).decode("utf-8")


def _query(parts) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(parts.query).items()}


def _transport(proxy: dict, q: dict[str, str]) -> None:
    network = q.get("type", "tcp")
    if network and network != "tcp":
        proxy["network"] = network
    if network == "ws":
        opts: dict = {"path": q.get("path", "/")}
        if q.get("host"):
            opts["headers"] = {"Host": q["host"]}
        proxy["ws-opts"] = opts
    elif network == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": q.get("serviceName", "")}


def _uri_to_proxy(line: str) -> dict:
    scheme = line.split("://", 1)[0].lower()
    if scheme == "vmess":
        data = json.loads(_b64(line.split("://", 1)[1].split("#")[0]))
        proxy = {"name": str(data.get("ps") or ""), "type": "vmess",
                 "server": str(data.get("add") or ""), "port": _port(data.get("port")),
                 "uuid": str(data.get("id") or ""), "alterId": _port(data.get("aid")) or 0,
                 "cipher": "auto", "udp": True}
        if str(data.get("tls") or "") == "tls":
            proxy["tls"] = True
            if data.get("sni"):
                proxy["servername"] = str(data["sni"])
        _transport(proxy, {"type": str(data.get("net") or "tcp"),
                           "path": str(data.get("path") or "/"),
                           "host": str(data.get("host") or "")})
        return proxy
    parts = urlsplit(line)
    name = unquote(parts.fragment)
    q = _query(parts)
    if scheme == "ss":
        if "@" in parts.netloc:
            userinfo, host = parts.netloc.rsplit("@", 1)
            try:
                method, _, password = _b64(userinfo).partition(":")
            except (binascii.Error, ValueError):
                method, _, password = unquote(userinfo).partition(":")
            server, _, port = host.rpartition(":")
        else:  # the whole authority is base64: method:password@host:port
            inner = _b64(parts.netloc + parts.path)
            cred, _, host = inner.rpartition("@")
            method, _, password = cred.partition(":")
            server, _, port = host.rpartition(":")
        return {"name": name, "type": "ss", "server": server.strip("[]"), "port": _port(port),
                "cipher": method, "password": password, "udp": True}
    server, port = parts.hostname or "", _port(parts.port) if _port_ok(parts) else None
    secret = unquote(parts.username or "")
    if scheme == "vless":
        proxy = {"name": name, "type": "vless", "server": server, "port": port, "uuid": secret,
                 "udp": True}
        security = q.get("security", "")
        if security in ("tls", "reality"):
            proxy["tls"] = True
        if q.get("sni"):
            proxy["servername"] = q["sni"]
        if q.get("flow"):
            proxy["flow"] = q["flow"]
        if q.get("fp"):
            proxy["client-fingerprint"] = q["fp"]
        if security == "reality":
            proxy["reality-opts"] = {"public-key": q.get("pbk", ""), "short-id": q.get("sid", "")}
        _transport(proxy, q)
        return proxy
    if scheme == "trojan":
        proxy = {"name": name, "type": "trojan", "server": server, "port": port,
                 "password": secret, "udp": True}
        if q.get("sni"):
            proxy["sni"] = q["sni"]
        _transport(proxy, q)
        return proxy
    proxy = {"name": name, "type": "hysteria2", "server": server, "port": port,
             "password": secret, "udp": True}
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if q.get("insecure") == "1":
        proxy["skip-cert-verify"] = True
    if q.get("obfs"):
        proxy["obfs"], proxy["obfs-password"] = q["obfs"], q.get("obfs-password", "")
    return proxy


def _port_ok(parts) -> bool:
    try:
        return parts.port is not None
    except ValueError:
        return False


def _uri_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()
            if line.strip().lower().split("://", 1)[0] in URI_SCHEMES and "://" in line]


def parse_subscription(body: bytes) -> list[dict]:
    """The proxy entries of a mihomo/Clash YAML or of a base64 (or plain) URI list."""
    try:
        text = body.decode("utf-8").lstrip("﻿").strip()
    except UnicodeDecodeError:
        raise Rejected("format") from None
    if not text:
        raise Rejected("format")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("proxies"), list):
        return list(data["proxies"])
    lines = _uri_lines(text)
    if not lines:
        try:
            lines = _uri_lines(_b64("".join(text.split())))
        except (binascii.Error, ValueError):
            lines = []
    if not lines:
        raise Rejected("format")
    try:
        return [_uri_to_proxy(line) for line in lines]
    except (binascii.Error, ValueError, KeyError, UnicodeDecodeError):
        raise Rejected("bad_node") from None


# ------------------------------------------------------------------ validation


@dataclass(frozen=True)
class Node:
    name: str
    type: str
    server: str
    port: int


@dataclass(frozen=True)
class Validated:
    """What a subscription turned out to be. Holds no URL."""

    nodes: tuple[Node, ...]
    """The real nodes."""
    names: tuple[str, ...]
    """Every entry's name, notices included (what the provider will list)."""
    provider_yaml: str
    digest: str
    protocols: dict[str, int] = field(default_factory=dict)

    @property
    def real_count(self) -> int:
        return len(self.nodes)


def normalize(entries: list[dict]) -> str:
    """The provider file's text: deterministic, so equal content has an equal digest."""
    return yaml.safe_dump({"proxies": entries}, sort_keys=True, allow_unicode=True,
                          default_flow_style=False)


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_of_file_text(text: str) -> str | None:
    """Digest of an existing provider file, ignoring how it was formatted."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if isinstance(data, dict) and isinstance(data.get("proxies"), list):
        return digest_of(normalize(list(data["proxies"])))
    return None


def names_of_file_text(text: str) -> list[str]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    proxies = data.get("proxies") if isinstance(data, dict) else None
    return [str(p.get("name") or "") for p in proxies or [] if isinstance(p, dict)]


def validate_content(body: bytes, *, min_nodes: int = 3, sentinel: re.Pattern[str] | None = None,
                     current_digest: str | None = None) -> Validated:
    entries = parse_subscription(body)
    if len(entries) > MAX_NODES:
        raise Rejected("too_many", str(len(entries)))
    cleaned: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
            raise Rejected("bad_node")
        entry = {**entry, "name": str(entry["name"]).strip()}
        kind = str(entry.get("type") or "").strip().lower()
        if kind in FORBIDDEN_TYPES:
            raise Rejected("forbidden_type", kind)
        cleaned.append(entry)
    names = [e["name"] for e in cleaned]
    if len(set(names)) != len(names):
        raise Rejected("dup_names")
    nodes: list[Node] = []
    for entry in cleaned:
        if not is_real_node(entry["name"], sentinel):
            continue
        kind = str(entry.get("type") or "").strip().lower()
        if kind not in ALLOWED_TYPES:
            raise Rejected("bad_type", kind or "?")
        server = str(entry.get("server") or "").strip()
        port = _port(entry.get("port"))
        if not server:
            raise Rejected("bad_server")
        if port is None or not 1 <= port <= 65535:
            raise Rejected("bad_port")
        nodes.append(Node(entry["name"], kind, server, port))
    if len(nodes) < min_nodes:
        raise Rejected("too_few", f"{len(nodes)}<{min_nodes}")
    text = normalize(cleaned)
    digest = digest_of(text)
    if current_digest and digest == current_digest:
        raise Rejected("same")
    return Validated(tuple(nodes), tuple(names), text, digest,
                     dict(Counter(n.type for n in nodes)))


def validate_url(url: str, *, fetch: Fetcher, is_direct: IsDirect, min_nodes: int = 3,
                 sentinel: re.Pattern[str] | None = None, allow_private: bool = False,
                 resolve: Resolver = system_resolve,
                 current_digest: str | None = None) -> tuple[Validated, Fetched]:
    """Check the URL, make sure mihomo routes its host DIRECT, fetch, parse, sanity-check."""
    host = check_url(url, allow_private=allow_private, resolve=resolve)
    if is_direct(host) is not True:
        raise Rejected("not_direct")  # never fetch through a node
    try:
        fetched = fetch(url)
    except FetchError as exc:
        raise Rejected("fetch", exc.code) from None
    if fetched.status != 200:
        raise Rejected("status", str(fetched.status))
    return validate_content(fetched.body, min_nodes=min_nodes, sentinel=sentinel,
                            current_digest=current_digest), fetched
