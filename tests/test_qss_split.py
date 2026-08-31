"""The QSS structural/palette split must not change a single byte.

``tests/fixtures/qss_parity.json`` records ``_substitute(theme.qss,
theme)``'s full output for all 16 bundled themes, captured at 3e04a70 —
before themes/_base.qss existed. The composed output must reproduce it
byte-for-byte. To change bundled styling on purpose, recapture and say
so in the commit:

    for each dir in theming.BUNDLED_THEMES_DIR:
        theme = theming._read_theme(dir)
        fixture[theme.slug] = theming._substitute(theme.qss, theme)

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_qss_split.py
"""
import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from tide import theming
from tide.ui import scale

FIXTURE = Path(__file__).parent / "fixtures" / "qss_parity.json"


def _bundled_themes() -> dict[str, theming.Theme]:
    """Bundled themes straight from the package dir — deliberately NOT
    discover_themes(), so a stray user/system override can't shadow the
    shipped files under test."""
    themes: dict[str, theming.Theme] = {}
    for child in sorted(theming.BUNDLED_THEMES_DIR.iterdir()):
        if not child.is_dir():
            continue
        theme = theming._read_theme(child)
        if theme is not None:
            themes[theme.slug] = theme
    return themes


def _write_theme_dir(root: Path, *, toml: str, qss: str | None) -> Path:
    d = root / "t"
    d.mkdir()
    (d / "theme.toml").write_text(toml, encoding="utf-8")
    if qss is not None:
        (d / "theme.qss").write_text(qss, encoding="utf-8")
    return d


class ParityTest(unittest.TestCase):
    """Composed output for every bundled theme == the pre-split capture."""

    @classmethod
    def setUpClass(cls) -> None:
        # @font_size substitutes through the UI scale; the fixture was
        # captured at NORMAL. Pin it for the module (the suite runs in one
        # process — an earlier test may have left another preset active).
        cls._saved_scale = scale.current()
        scale.set_factor(scale.Scale.NORMAL)
        with open(FIXTURE, encoding="utf-8") as f:
            cls.fixture: dict[str, str] = json.load(f)
        cls.themes = _bundled_themes()

    @classmethod
    def tearDownClass(cls) -> None:
        scale.set_factor(cls._saved_scale)

    def test_fixture_covers_exactly_the_bundled_themes(self) -> None:
        self.assertEqual(
            set(self.fixture), set(self.themes),
            "bundled theme set drifted from the parity fixture — a new or "
            "removed theme needs a deliberate fixture update",
        )
        self.assertEqual(len(self.themes), 16)

    def test_composed_output_is_byte_identical(self) -> None:
        for slug, want in sorted(self.fixture.items()):
            with self.subTest(theme=slug):
                theme = self.themes[slug]
                got = theming._substitute(theme.qss, theme)
                if got != want:   # pinpoint instead of dumping ~8KB twice
                    n = min(len(got), len(want))
                    i = next(
                        (k for k in range(n) if got[k] != want[k]), n)
                    self.fail(
                        f"{slug}: output diverges from fixture at char {i} "
                        f"(len {len(got)} vs {len(want)}):\n"
                        f"  want …{want[max(0, i - 70):i + 70]!r}…\n"
                        f"  got  …{got[max(0, i - 70):i + 70]!r}…"
                    )

    def test_every_bundled_theme_rides_the_base(self) -> None:
        """The migration is all-16: uses_base declared, overlay empty
        (palette lives in [tokens]), banner token present for the base's
        header comment."""
        for slug, theme in sorted(self.themes.items()):
            with self.subTest(theme=slug):
                with open(theme.path / "theme.toml", "rb") as f:
                    meta = tomllib.load(f).get("meta", {})
                self.assertIs(meta.get("uses_base"), True)
                self.assertFalse(
                    (theme.path / "theme.qss").exists(),
                    f"{slug} grew an overlay file — fine if intended, but "
                    "it appends after _base.qss and will break parity "
                    "until the fixture is deliberately recaptured",
                )
                self.assertTrue(str(theme.tokens.get("banner", "")).strip())

    def test_no_marker_lines_survive_composition(self) -> None:
        for slug, theme in sorted(self.themes.items()):
            with self.subTest(theme=slug):
                for line in theme.qss.split("\n"):
                    self.assertIsNone(theming._DIALECT_IF_RE.match(line))
                    self.assertIsNone(theming._DIALECT_ENDIF_RE.match(line))


class BaseFileLintTest(unittest.TestCase):
    """Shape checks on the shipped _base.qss itself."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = theming.BASE_QSS_PATH.read_text(encoding="utf-8")

    def test_markers_balance_without_nesting(self) -> None:
        depth = 0
        for n, line in enumerate(self.text.split("\n"), start=1):
            if theming._DIALECT_IF_RE.match(line):
                self.assertEqual(depth, 0, f"nested [if] at line {n}")
                depth = 1
            elif theming._DIALECT_ENDIF_RE.match(line):
                self.assertEqual(depth, 1, f"stray [endif] at line {n}")
                depth = 0
        self.assertEqual(depth, 0, "unclosed [if] block at EOF")

    def test_dialect_names_are_known(self) -> None:
        # "doc" is the always-stripped header block; everything else must
        # be a real aesthetic or its lines silently vanish for everyone.
        names = {
            m.group(1)
            for line in self.text.split("\n")
            if (m := theming._DIALECT_IF_RE.match(line))
        }
        self.assertEqual(names, {"doc", "brutalist", "modern"})

    def test_banner_line_present(self) -> None:
        self.assertIn("/* tide :: @banner */", self.text)


class DialectSelectTest(unittest.TestCase):
    SAMPLE = "\n".join([
        "shared-1",
        "/*[if brutalist]*/",
        "b-only",
        "/*[endif]*/",
        "/*[if modern]*/",
        "m-only-1",
        "m-only-2",
        "/*[endif]*/",
        "shared-2",
    ])

    def test_keeps_matching_block_drops_other(self) -> None:
        self.assertEqual(
            theming._select_dialect(self.SAMPLE, "brutalist"),
            "shared-1\nb-only\nshared-2")
        self.assertEqual(
            theming._select_dialect(self.SAMPLE, "modern"),
            "shared-1\nm-only-1\nm-only-2\nshared-2")

    def test_unknown_aesthetic_gets_shared_lines_only(self) -> None:
        self.assertEqual(
            theming._select_dialect(self.SAMPLE, "vaporwave"),
            "shared-1\nshared-2")

    def test_doc_block_always_stripped(self) -> None:
        text = "/*[if doc]*/\nprose\n/*[endif]*/\nreal"
        for aesthetic in ("brutalist", "modern"):
            self.assertEqual(theming._select_dialect(text, aesthetic), "real")

    def test_markers_must_own_the_whole_line(self) -> None:
        # Prose that merely mentions the syntax must pass through.
        line = "/* about /*[if brutalist]*/ markers */"
        self.assertEqual(
            theming._select_dialect(line, "modern"), line)

    def test_leading_whitespace_on_marker_ok(self) -> None:
        text = "  /*[if modern]*/\nkeep\n\t/*[endif]*/"
        self.assertEqual(theming._select_dialect(text, "modern"), "keep")

    def test_total_on_malformed_input(self) -> None:
        # Stray endif → reset to keep; missing endif → block runs to EOF.
        self.assertEqual(
            theming._select_dialect("/*[endif]*/\nx", "modern"), "x")
        self.assertEqual(
            theming._select_dialect("a\n/*[if brutalist]*/\nb", "modern"), "a")
        self.assertEqual(
            theming._select_dialect("a\n/*[if brutalist]*/\nb", "brutalist"),
            "a\nb")
        self.assertEqual(theming._select_dialect("", "modern"), "")


class CompositionTest(unittest.TestCase):
    """Loader-level behavior of uses_base vs legacy themes."""

    TOML = "\n".join([
        "[meta]",
        'name = "probe"',
        'slug = "probe"',
        "{extra}",
        "[tokens]",
        'bg = "#101010"',
        "[typography]",
        "mono = {mono}",
    ])

    def test_legacy_theme_without_flag_is_verbatim(self) -> None:
        qss = "/* mine */\nQLabel { color: @bg; }\n"
        with tempfile.TemporaryDirectory() as tmp:
            d = _write_theme_dir(
                Path(tmp),
                toml=self.TOML.format(extra="", mono="false"),
                qss=qss)
            theme = theming._read_theme(d)
        self.assertEqual(theme.qss, qss)

    def test_uses_base_appends_overlay_after_dialect_resolved_base(self) -> None:
        overlay = "QLabel { color: red; }\n"
        base_text = theming.BASE_QSS_PATH.read_text(encoding="utf-8")
        for mono, aesthetic in (("true", "brutalist"), ("false", "modern")):
            with self.subTest(aesthetic=aesthetic):
                with tempfile.TemporaryDirectory() as tmp:
                    d = _write_theme_dir(
                        Path(tmp),
                        toml=self.TOML.format(
                            extra="uses_base = true", mono=mono),
                        qss=overlay)
                    theme = theming._read_theme(d)
                self.assertEqual(theme.aesthetic, aesthetic)
                self.assertEqual(
                    theme.qss,
                    theming._select_dialect(base_text, aesthetic) + overlay)
                self.assertTrue(theme.qss.endswith(overlay))

    def test_uses_base_with_no_overlay_file_is_base_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = _write_theme_dir(
                Path(tmp),
                toml=self.TOML.format(extra="uses_base = true", mono="false"),
                qss=None)
            theme = theming._read_theme(d)
        base_text = theming.BASE_QSS_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            theme.qss, theming._select_dialect(base_text, "modern"))

    def test_uses_base_false_means_verbatim(self) -> None:
        qss = "QLabel { color: blue; }"
        with tempfile.TemporaryDirectory() as tmp:
            d = _write_theme_dir(
                Path(tmp),
                toml=self.TOML.format(extra="uses_base = false", mono="false"),
                qss=qss)
            theme = theming._read_theme(d)
        self.assertEqual(theme.qss, qss)


if __name__ == "__main__":
    unittest.main()
