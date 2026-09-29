"""Content-derived idempotency keys (design section 9.1).

The key comes from what is being shipped, never from a run id or counter, so
after a crash the planner produces the same key for the same order and the
simulator ignores the duplicate. The quantity in the key is the exact quantity
sent, so one key always means one body and IDEMPOTENCY_KEY_MISMATCH can't
happen. `part` separates equal-sized pieces of one split order.
"""

from __future__ import annotations

import base64
import hashlib


def idempotency_key(epoch: int, station: str, fuel: str, route: str, target_tick: int,
                    quantity: float, part: int = 0) -> str:
    raw = f"{epoch}|{station}|{fuel}|{route}|{target_tick}|{int(round(quantity))}|{part}"
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return "fsa-" + base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:24]
