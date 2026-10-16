"""The now-playing bar's customization model (2.3).

One JSON-able dict per personality (``Settings.bar_layout``, a stash
field): which controls show, their order, a size step each, what the
two info lines carry, where "next:" sits, the art size, the time
readout and the density. ``{}`` is "all defaults" — the bar every
release before 2.3 drew — so an upgrade changes nothing on screen.

Pure data, no Qt: :func:`normalize` is the only way in, and it never
raises on stale or hand-mangled state (unknown ids drop, missing ids
come back where they belong, bad enums fall back to defaults). The
window, the customizer popover and the settings row all speak it.
"""
from __future__ import annotations

import copy

# The bar's controls, in the shipped order. "|" is the gap: everything
# before it is the transport cluster, everything after it hugs the right
# edge (compact layouts: the transport row vs the tools row).
GAP = "|"
TRANSPORT: tuple[str, ...] = ("shuffle", "prev", "play", "next", "repeat", "like")
TOOLS: tuple[str, ...] = ("fx", "speed", "sleep", "fullscreen", "volume")
ITEMS: tuple[str, ...] = TRANSPORT + TOOLS
DEFAULT_ORDER: tuple[str, ...] = TRANSPORT + (GAP,) + TOOLS

# Play is the one thing the bar can't lose: hiding it would leave a
# player you can only drive from the keyboard. The editor greys its box.
ALWAYS_SHOWN: frozenset[str] = frozenset({"play"})

LABELS: dict[str, str] = {
    "shuffle": "shuffle",
    "prev": "previous",
    "play": "play / pause",
    "next": "next",
    "repeat": "repeat",
    "like": "like",
    "fx": "effects",
    "speed": "speed",
    "sleep": "sleep timer",
    "fullscreen": "fullscreen",
    "volume": "volume",
    GAP: "── right side ──",
}

SIZES: tuple[str, ...] = ("s", "m", "l")
SIZE_FACTORS: dict[str, float] = {"s": 0.8, "m": 1.0, "l": 1.3}

# Info-line vocabulary. "status" is tide's own voice (loading…, errors)
# and "source" the service the track came from.
FIELDS: tuple[str, ...] = (
    "title", "artist", "album", "plays", "likes", "year", "source", "status",
)
FIELD_LABELS: dict[str, str] = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "plays": "plays",
    "likes": "likes",
    "year": "year",
    "source": "source",
    "status": "status",
}

# What each now-label variant drew before the lines were editable.
# ``None`` in the stored config means "the variant's own" — so picking a
# different label style in the strip builder still re-shapes the lines
# until the user takes them over.
VARIANT_LINES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "stacked": (("artist", "title"),
                ("album", "plays", "likes", "year", "status")),
    "inline": (("artist", "title"), ()),
    "centered": (("title",), ("artist", "album")),
    "headline": (("title",),
                 ("artist", "album", "plays", "likes", "year", "status")),
}

NEXT_MODES: tuple[str, ...] = ("auto", "above", "below", "off")
ART_SIZES: dict[str, int] = {"off": 0, "s": 64, "m": 96, "l": 128}
TIME_MODES: tuple[str, ...] = ("both", "elapsed", "remaining", "off")
# The waveform seek bar (ui/waveform_seek.py): "on" = while loading, a
# few seconds after, and on hover; "hover" = only while seeking.
WAVEFORM_MODES: tuple[str, ...] = ("on", "hover", "off")
DENSITIES: dict[str, tuple[int, int, int]] = {
    # (outer margin h, outer margin v, row spacing)
    "tight": (10, 6, 3),
    "normal": (16, 12, 6),
    "roomy": (22, 18, 10),
}

CHOICE_LABELS: dict[str, dict[str, str]] = {
    "next": {"auto": "auto (personality)", "above": "above the title",
             "below": "under the title", "off": "hidden"},
    "art": {"off": "hidden", "s": "small", "m": "medium", "l": "large"},
    "time": {"both": "0:42 / 3:10", "elapsed": "0:42",
             "remaining": "−2:28", "off": "hidden"},
    "density": {"tight": "tight", "normal": "normal", "roomy": "roomy"},
    "waveform": {"on": "loading + seeking", "hover": "seeking only",
                 "off": "never"},
}


def defaults() -> dict:
    return {
        "order": list(DEFAULT_ORDER),
        "hidden": [],
        "sizes": {},
        "line1": None,
        "line2": None,
        "next": "auto",
        "art": "m",
        "time": "both",
        "density": "normal",
        "waveform": "on",
    }


def _fields(raw) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        return None
    out: list[str] = []
    for f in raw:
        f = str(f)
        if f in FIELDS and f not in out:
            out.append(f)
    return out


def normalize(raw) -> dict:
    """A complete, valid config from anything (``None``, ``{}``, a stale
    or mangled dict). Never raises; never returns a shared object."""
    out = defaults()
    if not isinstance(raw, dict):
        return out

    order_raw = raw.get("order")
    if isinstance(order_raw, (list, tuple)):
        order: list[str] = []
        for item in order_raw:
            item = str(item)
            if (item in ITEMS or item == GAP) and item not in order:
                order.append(item)
        if GAP not in order:
            # The gap was lost: put it after the last transport id seen.
            last_t = max((i for i, x in enumerate(order) if x in TRANSPORT),
                         default=-1)
            order.insert(last_t + 1, GAP)
        # Ids a newer default knows but this config predates: back into
        # their home cluster, after its last member already placed.
        for item in ITEMS:
            if item in order:
                continue
            if item in TRANSPORT:
                order.insert(order.index(GAP), item)
            else:
                order.append(item)
        out["order"] = order

    hidden = raw.get("hidden")
    if isinstance(hidden, (list, tuple)):
        out["hidden"] = [h for h in dict.fromkeys(str(x) for x in hidden)
                         if h in ITEMS and h not in ALWAYS_SHOWN]

    sizes = raw.get("sizes")
    if isinstance(sizes, dict):
        out["sizes"] = {str(k): str(v) for k, v in sizes.items()
                        if str(k) in ITEMS and str(v) in SIZES and str(v) != "m"}

    for key in ("line1", "line2"):
        if key in raw:
            out[key] = _fields(raw.get(key))

    for key, allowed in (("next", NEXT_MODES), ("art", tuple(ART_SIZES)),
                         ("time", TIME_MODES), ("density", tuple(DENSITIES)),
                         ("waveform", WAVEFORM_MODES)):
        value = raw.get(key)
        if isinstance(value, str) and value in allowed:
            out[key] = value
    return out


def is_default(cfg) -> bool:
    return normalize(cfg) == defaults()


def visible_order(cfg) -> list[str]:
    """The order with hidden ids dropped (the gap stays)."""
    cfg = normalize(cfg)
    hidden = set(cfg["hidden"])
    return [x for x in cfg["order"] if x == GAP or x not in hidden]


def clusters(cfg) -> tuple[list[str], list[str]]:
    """(left cluster, right cluster) of the visible ids."""
    order = visible_order(cfg)
    i = order.index(GAP)
    return order[:i], order[i + 1:]


def size_of(cfg, item: str) -> str:
    return normalize(cfg)["sizes"].get(item, "m")


def size_factor(cfg, item: str) -> float:
    return SIZE_FACTORS[size_of(cfg, item)]


def lines_for(cfg, variant: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The (line 1, line 2) fields for a now-label ``variant`` — the
    user's lists where set, else the variant's own."""
    cfg = normalize(cfg)
    base1, base2 = VARIANT_LINES.get(variant, VARIANT_LINES["stacked"])
    l1 = tuple(cfg["line1"]) if cfg["line1"] is not None else base1
    l2 = tuple(cfg["line2"]) if cfg["line2"] is not None else base2
    return l1, l2


def with_change(cfg, **changes) -> dict:
    """A normalized copy with ``changes`` applied (the editor's verb)."""
    out = copy.deepcopy(normalize(cfg))
    out.update(changes)
    return normalize(out)


def set_shown(cfg, item: str, shown: bool) -> dict:
    cfg = normalize(cfg)
    hidden = [h for h in cfg["hidden"] if h != item]
    if not shown and item not in ALWAYS_SHOWN:
        hidden.append(item)
    return with_change(cfg, hidden=hidden)


def set_size(cfg, item: str, size: str) -> dict:
    cfg = normalize(cfg)
    sizes = dict(cfg["sizes"])
    sizes[item] = size
    return with_change(cfg, sizes=sizes)


def move(cfg, item: str, to_index: int) -> dict:
    """Move ``item`` (an id or the gap) to ``to_index`` in the order."""
    cfg = normalize(cfg)
    order = list(cfg["order"])
    if item not in order:
        return cfg
    order.remove(item)
    to_index = max(0, min(int(to_index), len(order)))
    order.insert(to_index, item)
    return with_change(cfg, order=order)


def format_time(mode: str, position: float, duration: float,
                fmt=None) -> str:
    """The time readout's text for ``mode`` ("" when hidden)."""
    def _mmss(s: float) -> str:
        s = max(0, int(s))
        m, sec = divmod(s, 60)
        return f"{m}:{sec:02d}"
    fmt = fmt or _mmss
    if mode == "off":
        return ""
    if mode == "elapsed":
        return fmt(position)
    if mode == "remaining":
        return "−" + fmt(max(0.0, duration - position))
    return f"{fmt(position)} / {fmt(duration)}"
