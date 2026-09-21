"""The palette follows the Chenpon site (teal #00C5CE). That teal alone is too light for text or for
white-on-colour buttons (contrast about 2:1), so the interactive colour is a darker teal and the brand
teal is used only as decoration. These checks keep every text/background pair readable (WCAG AA)."""
import re
import unittest
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "assets" / "style.css").read_text(encoding="utf-8")


def tokens(selector_start: str) -> dict[str, str]:
    block = re.search(re.escape(selector_start) + r"\{([^}]*)\}", CSS).group(1)
    return dict(re.findall(r"--([a-z-]+):([^;]+)", block))


def rgb(value: str) -> tuple[float, float, float]:
    value = value.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", value):
        value = "#" + "".join(c * 2 for c in value[1:])
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def luminance(color: tuple[float, float, float]) -> float:
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(rgb(a)), luminance(rgb(b))), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class ThemeContrastTests(unittest.TestCase):
    light = tokens(":root")
    dark = {**tokens(":root"), **tokens("html[data-theme=dark]")}

    def test_every_text_pair_is_readable_in_both_themes(self):
        for name, theme in (("light", self.light), ("dark", self.dark)):
            for foreground, background, needed in (
                ("text", "bg", 7.0), ("text", "panel", 7.0), ("muted", "bg", 4.5), ("muted", "panel", 4.5),
                ("accent", "bg", 4.5), ("accent", "panel", 4.5), ("on-accent", "accent", 4.5),
            ):
                ratio = contrast(theme[foreground], theme[background])
                self.assertGreaterEqual(ratio, needed, f"{name}: {foreground} on {background} is {ratio:.2f}:1")

    def test_the_brand_teal_is_the_one_from_the_company_site_and_only_decorative(self):
        self.assertEqual(self.light["brand"].lower(), "#00c5ce")
        self.assertEqual(self.dark["brand"].lower(), "#00c5ce")
        self.assertLess(contrast(self.light["brand"], self.light["bg"]), 3.0)      # why it must not carry text
        self.assertNotEqual(self.light["accent"].lower(), self.light["brand"].lower())

    def test_no_white_text_is_left_on_the_accent_colour(self):
        for match in re.finditer(r"\{([^{}]*background:var\(--accent\)[^{}]*)\}", CSS):
            self.assertNotIn("color:#fff", match.group(1).replace("background-color", ""), match.group(1)[:80])

    def test_the_old_blue_is_gone_from_the_stylesheet(self):
        self.assertNotIn("#0075de", CSS)
        self.assertNotIn("#62aef0", CSS)


if __name__ == "__main__":
    unittest.main()
