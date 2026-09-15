"""OneBot implementation compatibility layer (NapCat / LLOneBot / SnowLuma).

The three implementations expose the same two history actions but disagree on
almost everything else - parameter name, direction and page size. This module
probes `get_version_info` once and returns a knob dict the scanner uses.

No framework imports: unit-testable standalone.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# app_name returned by get_version_info -> knobs.
_IMPLS: Dict[str, Dict[str, Any]] = {
    "napcat": {
        "app_name": "NapCat.Onebot",
        "anchor_field": "message_seq",
        "anchor_param": "message_seq",
        "order_param": "reverse_order",
        "order_backwards": True,
        "max_page": 200,
        "max_scan_limit": 2000,
    },
    "llonebot": {
        "app_name": "LLOneBot",
        "anchor_field": "message_seq",
        "anchor_param": "message_seq",
        "order_param": "reverseOrder",
        "order_backwards": True,
        "max_page": 30,          # getMsgsBySeqAndCount is hard-capped at 30
        "max_scan_limit": 1200,
    },
    "snowluma": {
        "app_name": "SnowLuma",
        "anchor_field": "message_id",   # negative int32 hash - NOT message_seq
        "anchor_param": "message_id",
        "order_param": "reverse_order",
        "order_backwards": True,
        "max_page": 200,
        "max_scan_limit": 800,          # 300ms server-side gate serialises pages
    },
}

# Used until (or if) the probe succeeds. Deliberately the most conservative
# knob set: no anchor → "fetch latest N" works everywhere, and the documented
# go-cqhttp order parameter name is the safest of the three.
_GENERIC: Dict[str, Any] = {
    "app_name": "unknown",
    "anchor_field": "message_seq",
    "anchor_param": None,            # None = never send an anchor
    "order_param": "reverse_order",
    "order_backwards": True,
    "max_page": 30,
    "max_scan_limit": 600,
}

_PREFIXES = (
    ("napcat", "napcat"),
    ("llonebot", "llonebot"),
    ("llob", "llonebot"),
    ("snowluma", "snowluma"),
    ("snow", "snowluma"),
)


def _classify(app_name: str) -> Optional[str]:
    low = (app_name or "").strip().lower()
    if not low:
        return None
    for prefix, key in _PREFIXES:
        if prefix in low:
            return key
    return None


def resolve_impl(app_name: str) -> Dict[str, Any]:
    key = _classify(app_name)
    if key is None:
        return dict(_GENERIC)
    return dict(_IMPLS[key])


def build_payload(
    impl: Dict[str, Any],
    base: Dict[str, Any],
    anchor: Any,
    count: int,
) -> Dict[str, Any]:
    """Build the action payload for one page.

    Extra keys are harmless (verified by source review + a typebox/schemastery
    harness): NapCat's `Value.Parse`+`Check` keeps unknown keys, LLOneBot's
    schemastery keeps them too, and SnowLuma's kit only reads the fields it
    declares. So we can send one payload that satisfies all three.
    """
    payload = dict(base)
    payload["count"] = int(count)
    if impl.get("anchor_param") and anchor is not None and str(anchor) != "":
        payload[impl["anchor_param"]] = anchor
        order_param = impl.get("order_param")
        if order_param:
            payload[order_param] = bool(impl.get("order_backwards", True))
    return payload
