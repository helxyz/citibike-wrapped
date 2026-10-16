"""Redraw output/wrapped_YEAR.html from template.html + saved numbers (no recalculation)."""
import json, sys
from pathlib import Path

year = sys.argv[1] if len(sys.argv) > 1 else "2025"
root = Path(__file__).resolve().parent
data = json.loads((root / "output" / f"wrapped_{year}.json").read_text())
head = ('<!doctype html>\n<html lang="en"><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n')
html = (root / "template.html").read_text().replace(
    "/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
(root / "output" / f"wrapped_{year}.html").write_text(head + html)
print(f"redrawn output/wrapped_{year}.html")