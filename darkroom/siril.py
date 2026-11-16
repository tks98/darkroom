"""Render the Siril script templates in siril/ and run them with siril-cli."""
from __future__ import annotations

import re
from pathlib import Path

from . import tools

_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def template_dir() -> Path:
    here = Path(__file__).resolve().parent
    for d in (here / "siril", here.parent / "siril"):   # wheel install, then source checkout
        if d.is_dir():
            return d
    raise tools.StageError("Siril script templates not found (expected darkroom/siril or ../siril)")


def fmt_num(x: float) -> str:
    """Numbers as Siril should see them: 25.0 -> '25', -2.5 -> '-2.5'."""
    return format(float(x), "g")


def render(template: str, values: dict[str, str]) -> str:
    """Replace {{NAME}} placeholders. Every placeholder must have a value and vice versa."""
    found = set(_PLACEHOLDER.findall(template))
    missing = found - values.keys()
    if missing:
        raise KeyError(f"no value for placeholder(s): {sorted(missing)}")
    unused = values.keys() - found
    if unused:
        raise KeyError(f"value(s) not used by template: {sorted(unused)}")
    for k, v in values.items():
        if '"' in v:
            raise ValueError(f"{k} contains a double quote, which Siril scripts cannot quote: {v!r}")
    out = _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
    if "{{" in out or "}}" in out:
        raise ValueError("unrendered placeholder left in Siril script")
    return out


def render_file(name: str, work: Path, values: dict[str, str]) -> Path:
    """Render siril/<name> to <work>/logs/<name> and return its absolute path."""
    text = render((template_dir() / name).read_text(), values)
    out = (tools.logs_dir(work) / name).resolve()
    out.write_text(text)
    return out


def run_script(script: Path, log_path: Path) -> None:
    exe = tools.require_executable(tools.siril_cli(), "DARKROOM_SIRIL")
    log_path.write_text("")
    tools.run_tool([str(exe), "-s", str(script.resolve())], log_path)
    # siril-cli can exit 0 after a failed command; catch the common message.
    text = log_path.read_text(errors="replace")
    if "Script execution failed" in text or "Error in line" in text:
        raise tools.StageError(f"Siril script {script.name} failed; last lines of {log_path}:\n"
                               + tools.tail(log_path))
