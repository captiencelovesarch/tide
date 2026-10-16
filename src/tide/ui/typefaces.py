"""Typography roles (2.3): one family / weight / size / spacing per role.

Three roles:
  * ``ui``      — every widget's body text. Family and size are the
                  long-standing ``font_family_override`` /
                  ``font_size_override_pt`` settings; this module adds a
                  weight and letter spacing on top.
  * ``title``   — the track title in the bar (and the fullscreen /
                  mini headlines that paint through :func:`font_for`),
                  plus QSS ``QLabel.title`` / ``QLabel.display``.
  * ``numbers`` — time readouts, the speed pill and bubble, counts.

State is one dict per personality (``Settings.font_roles``, a stash
field), pushed in with :func:`set_roles`. Painted widgets ask
:func:`font_for` at paint time; QLabels/buttons get theirs from the
stylesheet rules :func:`extra_qss` contributes to every theme restyle,
matched on a ``fontRole`` dynamic property. Defaults change nothing:
an empty family / 0 weight / 100% size / 0 spacing is the theme's own.

Qt stylesheets can't express letter spacing, so spacing reaches the
QSS-styled widgets through the font they carry (:func:`tag`), which the
stylesheet's family/size rules merge over without dropping it.
"""
from __future__ import annotations

import copy

ROLES: tuple[str, ...] = ("ui", "title", "numbers")
ROLE_LABELS: dict[str, str] = {
    "ui": "body",
    "title": "titles",
    "numbers": "numbers",
}
WEIGHTS: tuple[tuple[int, str], ...] = (
    (0, "from theme"),
    (300, "light"),
    (400, "regular"),
    (500, "medium"),
    (600, "semibold"),
    (700, "bold"),
    (800, "heavy"),
)
SIZE_RANGE = (60, 200)        # percent of the body size
SPACING_RANGE = (-10, 40)     # percent added to normal tracking

# The bundled faces — always present, pinned first in the browser.
BUNDLED: tuple[str, ...] = ("Inter", "IBM Plex Sans", "IBM Plex Mono",
                            "JetBrains Mono")

# One-click pairings, per personality. "family" for ``ui`` is written to
# font_family_override by whoever applies a pairing (the settings row);
# everything here is bundled so a pairing never lands on a missing face.
PAIRINGS: dict[str, tuple[tuple[str, str, dict], ...]] = {
    "modern": (
        ("theme", "the theme's own", {}),
        ("inter", "inter · jetbrains numbers", {
            "ui": {"family": "Inter"},
            "title": {"family": "Inter", "weight": 600, "size": 110,
                      "spacing": -2},
            "numbers": {"family": "JetBrains Mono"},
        }),
        ("plex", "plex sans · plex mono numbers", {
            "ui": {"family": "IBM Plex Sans"},
            "title": {"family": "IBM Plex Sans", "weight": 600},
            "numbers": {"family": "IBM Plex Mono"},
        }),
        ("editorial", "editorial · big plex titles", {
            "ui": {"family": "Inter", "weight": 400},
            "title": {"family": "IBM Plex Sans", "weight": 700, "size": 125,
                      "spacing": -3},
            "numbers": {"family": "Inter", "weight": 500},
        }),
        ("airy", "airy · light + wide", {
            "ui": {"family": "Inter", "weight": 300, "spacing": 4},
            "title": {"family": "Inter", "weight": 500, "spacing": 2},
            "numbers": {"family": "IBM Plex Mono", "weight": 400},
        }),
    ),
    "brutalist": (
        ("theme", "the theme's own", {}),
        ("plex", "plex mono · bold titles", {
            "ui": {"family": "IBM Plex Mono"},
            "title": {"family": "IBM Plex Mono", "weight": 700},
            "numbers": {"family": "IBM Plex Mono"},
        }),
        ("jetbrains", "jetbrains mono everywhere", {
            "ui": {"family": "JetBrains Mono"},
            "title": {"family": "JetBrains Mono", "weight": 700},
            "numbers": {"family": "JetBrains Mono"},
        }),
        ("terminal", "terminal · wide tracking", {
            "ui": {"family": "IBM Plex Mono", "spacing": 6},
            "title": {"family": "JetBrains Mono", "weight": 700,
                      "spacing": 10},
            "numbers": {"family": "JetBrains Mono", "weight": 700},
        }),
        ("poster", "poster · sans headlines", {
            "ui": {"family": "IBM Plex Mono"},
            "title": {"family": "IBM Plex Sans", "weight": 700, "size": 130,
                      "spacing": -2},
            "numbers": {"family": "IBM Plex Mono", "weight": 500},
        }),
    ),
}


def _role_default() -> dict:
    return {"family": "", "weight": 0, "size": 100, "spacing": 0}


def defaults() -> dict:
    return {role: _role_default() for role in ROLES}


def _int(v, lo: int, hi: int, fallback: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, n))


def normalize(raw) -> dict:
    """A complete roles dict from anything. Never raises."""
    out = defaults()
    if not isinstance(raw, dict):
        return out
    weights = {w for w, _ in WEIGHTS}
    for role in ROLES:
        r = raw.get(role)
        if not isinstance(r, dict):
            continue
        cur = out[role]
        fam = r.get("family", "")
        cur["family"] = fam.strip() if isinstance(fam, str) else ""
        w = _int(r.get("weight", 0), 0, 900, 0)
        cur["weight"] = w if w in weights else 0
        cur["size"] = _int(r.get("size", 100), *SIZE_RANGE, 100)
        cur["spacing"] = _int(r.get("spacing", 0), *SPACING_RANGE, 0)
    # The body's size is font_size_override_pt; a stray percent here
    # would double-scale every widget.
    out["ui"]["size"] = 100
    return out


def compact(roles) -> dict:
    """``roles`` minus default entries — what gets persisted."""
    roles = normalize(roles)
    base = _role_default()
    out: dict = {}
    for role, spec in roles.items():
        diff = {k: v for k, v in spec.items() if v != base[k]}
        if diff:
            out[role] = diff
    return out


def is_default(roles) -> bool:
    return not compact(roles)


_roles: dict = defaults()


def set_roles(raw) -> bool:
    """Install the active roles. True when anything changed."""
    global _roles
    new = normalize(raw)
    if new == _roles:
        return False
    _roles = new
    return True


def roles() -> dict:
    return copy.deepcopy(_roles)


def role(name: str) -> dict:
    return dict(_roles.get(name, _role_default()))


def pairings_for(personality: str) -> tuple[tuple[str, str, dict], ...]:
    return PAIRINGS.get(personality, PAIRINGS["modern"])


def pairing(personality: str, slug: str) -> dict | None:
    """The full roles dict for a pairing (``ui.family`` included), or
    None for an unknown slug."""
    for s, _label, spec in pairings_for(personality):
        if s == slug:
            out = defaults()
            for r, vals in spec.items():
                out[r].update(vals)
            return normalize(out)
    return None


def matching_pairing(personality: str, roles_value, ui_family: str) -> str:
    """The pairing slug that ``roles_value`` + ``ui_family`` spell, or ""."""
    cur = normalize(roles_value)
    cur["ui"]["family"] = (ui_family or "").strip()
    for slug, _label, _spec in pairings_for(personality):
        p = pairing(personality, slug)
        if p is not None and p == cur:
            return slug
    return ""


# ---------------------------------------------------------------- Qt side

def font_for(name: str, base):
    """``base`` (a QFont) dressed in role ``name``. The ui role's family
    and size already live in the app font, so only weight/spacing apply
    for it; title/numbers swap family, scale size, set weight/spacing."""
    from PySide6.QtGui import QFont
    f = QFont(base)
    spec = _roles.get(name)
    if not spec:
        return f
    if name != "ui" and spec["family"]:
        f.setFamily(spec["family"])
        f.setFamilies([spec["family"]])
    if spec["weight"]:
        f.setWeight(QFont.Weight(spec["weight"]))
    if name != "ui" and spec["size"] != 100:
        pt = f.pointSizeF()
        if pt > 0:
            f.setPointSizeF(max(1.0, pt * spec["size"] / 100.0))
        elif f.pixelSize() > 0:
            f.setPixelSize(max(1, round(f.pixelSize() * spec["size"] / 100.0)))
    if spec["spacing"]:
        f.setLetterSpacing(QFont.PercentageSpacing, 100.0 + spec["spacing"])
    return f


def apply_to_app_font(font):
    """The ui role's weight + spacing on the app font (theming's apply).
    Returns the font to install, possibly ``font`` itself."""
    spec = _roles["ui"]
    if not spec["weight"] and not spec["spacing"]:
        return font
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    if font is None:
        app = QApplication.instance()
        font = QFont(app.font()) if app is not None else QFont()
    else:
        font = QFont(font)
    if spec["weight"]:
        font.setWeight(QFont.Weight(spec["weight"]))
    if spec["spacing"]:
        font.setLetterSpacing(QFont.PercentageSpacing, 100.0 + spec["spacing"])
    return font


def tag(widget, name: str) -> None:
    """Mark a QSS-styled widget as carrying role ``name``: the
    stylesheet rules from :func:`extra_qss` match the property, and the
    spacing (which QSS can't say) rides the widget's own font."""
    widget.setProperty("fontRole", name)
    retint(widget)


def retint(widget) -> None:
    """Re-push the role's spacing onto a tagged widget (after a change)."""
    from PySide6.QtGui import QFont
    name = widget.property("fontRole")
    if not name:
        return
    spec = _roles.get(str(name))
    f = QFont(widget.font())
    spacing = 100.0 + (spec["spacing"] if spec else 0)
    if abs(f.letterSpacing() - spacing) > 1e-6 or \
            f.letterSpacingType() != QFont.PercentageSpacing:
        f.setLetterSpacing(QFont.PercentageSpacing, spacing)
        widget.setFont(f)
    st = widget.style()
    if st is not None:
        st.unpolish(widget)
        st.polish(widget)


def _qss_family(family: str) -> str:
    return '"' + family.replace('"', "") + '"'


def extra_qss(base_pt: float) -> str:
    """Stylesheet rules for the title / numbers roles, appended to the
    theme's sheet on every restyle. ``base_pt`` is the body size the
    sheet's ``*`` rule uses (already ui-scaled)."""
    rules: list[str] = []
    for name, selectors in (
        ("title", ('*[fontRole="title"]', "QLabel.title", "QLabel.display")),
        ("numbers", ('*[fontRole="numbers"]',)),
    ):
        spec = _roles[name]
        props: list[str] = []
        if spec["family"]:
            props.append(f"font-family: {_qss_family(spec['family'])};")
        if spec["weight"]:
            props.append(f"font-weight: {int(spec['weight'])};")
        if spec["size"] != 100 and name == "numbers":
            # title sizes ride the theme's own type scale for QLabel.title;
            # only the role-tagged widgets take the percent.
            props.append(f"font-size: {max(1.0, base_pt * spec['size'] / 100):.1f}pt;")
        if not props:
            continue
        rules.append(f"{', '.join(selectors)} {{ {' '.join(props)} }}")
        if name == "title" and spec["size"] != 100:
            rules.append(
                f'*[fontRole="title"] {{ font-size: '
                f"{max(1.0, base_pt * spec['size'] / 100):.1f}pt; }}")
    # The ui role's weight + spacing ride the app font
    # (apply_to_app_font): the sheet's ``*`` rule never says weight.
    return ("\n/* tide :: typography roles */\n" + "\n".join(rules) + "\n") \
        if rules else ""
