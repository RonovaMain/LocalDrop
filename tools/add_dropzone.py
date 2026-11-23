"""CI helper: add the optional flet-dropzone extension to pyproject.toml (desktop builds only)."""
import re
from pathlib import Path

p = Path(__file__).resolve().parents[1] / "pyproject.toml"
text = p.read_text(encoding="utf-8")
if "flet-dropzone" not in text:
    text = re.sub(r'("segno>=[^"]*",\n)', r'\1    "flet-dropzone>=0.3.1",\n', text, count=1)
    p.write_text(text, encoding="utf-8")
print("dropzone dependency present:", "flet-dropzone" in p.read_text(encoding="utf-8"))
