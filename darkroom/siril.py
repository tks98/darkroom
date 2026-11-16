"""Render the Siril script templates in siril/ and run them with siril-cli.

Template syntax (deliberately tiny):

  {{NAME}}            required placeholder
  {{NAME=default}}    optional placeholder; ``default`` is used when no value is given
  {{if NAME}}line     keep ``line`` only when NAME has a non-empty value
  {{ifnot NAME}}line  keep ``line`` only when NAME is missing or empty

Values must not contain double quotes (Siril scripts cannot quote them).
"""
from __future__ import annotations

import re
from pathlib import Path

from . import tools

_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)(?:=([^{}]*))?\}\}")
_COND = re.compile(r"^\{\{(if|ifnot) ([A-Z_]+)\}\}")


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
    """Replace placeholders. Every required placeholder must have a value, every value a use."""
    lines, cond_names = [], set()
    for line in template.splitlines(keepends=True):
        m = _COND.match(line)
        if m:
            kind, name = m.groups()
            cond_names.add(name)
            on = bool(values.get(name))
            if (kind == "if") != on:
                continue
            line = line[m.end():]
        lines.append(line)
    body = "".join(lines)

    required, optional = set(), set()
    for m in _PLACEHOLDER.finditer(template):          # whole template: names in dropped lines count
        (optional if m.group(2) is not None else required).add(m.group(1))
    missing = required - values.keys()
    if missing:
        raise KeyError(f"no value for placeholder(s): {sorted(missing)}")
    unused = values.keys() - required - optional - cond_names
    if unused:
        raise KeyError(f"value(s) not used by template: {sorted(unused)}")
    for k, v in values.items():
        if '"' in v:
            raise ValueError(f"{k} contains a double quote, which Siril scripts cannot quote: {v!r}")

    def sub(m):
        name, default = m.group(1), m.group(2)
        if name in values:
            return values[name]
        return default
    out = _PLACEHOLDER.sub(sub, body)
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
