import json
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).parents[1] / "assets" / "app.js"
STYLE_CSS = Path(__file__).parents[1] / "assets" / "style.css"


class ColumnWidthTests(unittest.TestCase):
    def run_javascript(self, expression):
        script = f"const c=require({json.dumps(str(APP_JS))}); process.stdout.write(JSON.stringify({expression}));"
        return json.loads(subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True).stdout)

    def test_a_dragged_width_is_held_between_48_and_2000_pixels(self):
        for value, expected in ((10, 48), (48, 48), (300.4, 300), (300.6, 301), ("x", 48), (None, 48), (99999, 2000)):
            with self.subTest(value=value):
                self.assertEqual(self.run_javascript(f"c.clampColumnWidth({json.dumps(value)})"), expected)

    def test_stored_widths_are_used_only_when_they_fit_this_table(self):
        self.assertEqual(self.run_javascript("c.readStoredWidths('[60,200]', 2)"), [60, 200])
        for text, count in (("[60,200]", 3), ("[60,1]", 2), ("[60,99999]", 2), ('[60,"a"]', 2), ("nope", 2), ("null", 2), ("{}", 2)):
            with self.subTest(text=text, count=count):
                self.assertIsNone(self.run_javascript(f"c.readStoredWidths({json.dumps(text)}, {count})"))


class ResizerWiringTests(unittest.TestCase):
    script = APP_JS.read_text(encoding="utf-8")
    style = STYLE_CSS.read_text(encoding="utf-8")

    def test_every_table_with_a_header_row_is_made_resizable_and_the_home_list_too(self):
        self.assertIn('document.querySelectorAll("table").forEach((table, index) => makeTableResizable(table, index));', self.script)
        self.assertIn('makeGridResizable(document.querySelector(".recent-table"));', self.script)

    def test_tables_the_handles_cannot_serve_are_left_alone(self):
        self.assertIn("heads.length < 2 || heads.some(cell => cell.colSpan > 1) || table.dataset.resizable", self.script)

    def test_widths_are_remembered_per_page_and_table_and_double_click_resets(self):
        self.assertIn("`dek-cols:${location.pathname}:${index}:", self.script)
        self.assertIn('handle.addEventListener("dblclick"', self.script)
        self.assertIn("forgetWidths(key)", self.script)
        self.assertIn('event.key !== "ArrowLeft" && event.key !== "ArrowRight"', self.script)      # keyboard-adjustable too

    def test_storage_failures_never_break_the_page(self):
        for helper, call in (("storedWidths", "localStorage.getItem(key)"), ("storeWidths", "localStorage.setItem(key, JSON.stringify(widths))"),
                             ("forgetWidths", "localStorage.removeItem(key)")):
            line = next(line for line in self.script.splitlines() if f"const {helper} = " in line)
            self.assertIn(call, line)
            self.assertIn("try {", line)
            self.assertIn("catch (error)", line)

    def test_the_handles_have_styles_and_a_hidden_state_that_really_hides(self):
        for rule in (".col-resizer{position:absolute;", "table.resizable{display:table}", ".table-scroll{max-width:100%;overflow-x:auto}",
                     ".recent-head .col-resizer{display:none}"):
            self.assertIn(rule, self.style)

    def test_the_review_pages_load_the_shared_script_and_styles(self):
        review = (Path(__file__).parents[1] / "review.py").read_text(encoding="utf-8")
        self.assertIn('<link rel="stylesheet" href="/assets/style.css">', review)
        self.assertIn('<script src="/assets/app.js" defer></script>', review)


if __name__ == "__main__":
    unittest.main()
