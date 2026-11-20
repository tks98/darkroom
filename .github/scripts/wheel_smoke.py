"""Check an installed (non-editable) darkroom: it is imported from site-packages, finds the Siril
script templates and presets shipped inside the wheel, and can render every template.

Run it with the installed interpreter, isolated and outside the checkout:
    cd "$RUNNER_TEMP" && /path/to/venv/bin/python -I wheel_smoke.py
"""
import re
import sys
from pathlib import Path

import darkroom
from darkroom import config, siril

pkg = Path(darkroom.__file__).resolve().parent
assert "site-packages" in pkg.parts, f"darkroom imported from {pkg}, not from an installed wheel"
assert siril.template_dir() == pkg / "siril", f"templates from {siril.template_dir()}, not the wheel"
assert config.presets_dir() == pkg / "presets", f"presets from {config.presets_dir()}, not the wheel"

templates = sorted(siril.template_dir().glob("*.ssf"))
assert {"stack.ssf", "color.ssf", "stretch.ssf"} <= {t.name for t in templates}, templates
for t in templates:
    text = t.read_text()
    names = {m.group(1) for m in siril._PLACEHOLDER.finditer(text)}
    names |= set(re.findall(r"\{\{(?:if|ifnot) ([A-Z_]+)\}\}", text))
    out = siril.render(text, {n: "1" for n in names})
    assert "{{" not in out and out.strip(), t.name

cameras, looks = config.preset_names("cameras"), config.preset_names("looks")
assert "seestar-s30-pro" in cameras and "galaxy" in looks, (cameras, looks)
for name in cameras:
    config.load_camera(name)
print(f"darkroom {darkroom.__version__} from {pkg}: {len(templates)} templates rendered; "
      f"presets: cameras {', '.join(cameras)}; looks {', '.join(looks)}")
sys.exit(0)
