from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import textwrap
import threading
import time
import uuid
import zipfile
from io import BytesIO
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from easyeda2kicad._version import GENERATOR_URL
from easyeda2kicad.easyeda.easyeda_api import EasyedaApi
from easyeda2kicad.easyeda.easyeda_importer import (
    Easyeda3dModelImporter,
    EasyedaFootprintImporter,
    EasyedaSymbolImporter,
)
from easyeda2kicad.kicad.export_kicad_3d_model import Exporter3dModelKicad
from easyeda2kicad.kicad.export_kicad_footprint import ExporterFootprintKicad
from easyeda2kicad.kicad.export_kicad_symbol import ExporterSymbolKicad
from easyeda2kicad.kicad.parameters_kicad_symbol import (
    KICAD_SYM_VERSION_20251024,
    KiPinStyle,
    KiPinType,
    KiSymbol,
    KiSymbolInfo,
    KiSymbolPin,
    KiSymbolRectangle,
    sanitize_fields,
)

KICAD_VERSION = KICAD_SYM_VERSION_20251024
GENERATOR = "jlc_kicad_symbol_editor"
DEFAULT_LIBRARY = "easyeda2kicad"
PIN_SPACING = 2.54
PIN_LENGTH = 2.54
CODEX_TIMEOUT_SECONDS = 600
CODEX_STATUS_INTERVAL_SECONDS = 5
CODEX_PROFILE_ENV = "SYMBOL_EDITOR_CODEX_PROFILE"
DEFAULT_CODEX_PROFILE = "work"

PIN_TYPE_VALUES = [
    "input",
    "output",
    "bidirectional",
    "tri_state",
    "passive",
    "free",
    "unspecified",
    "power_in",
    "power_out",
    "open_collector",
    "open_emitter",
    "no_connect",
]

PIN_STYLE_VALUES = [
    "line",
    "inverted",
    "clock",
    "inverted_clock",
    "input_low",
    "clock_low",
    "output_low",
    "edge_clock_high",
    "non_logic",
]

_PIN_TYPE_TO_ENUM = {
    "input": KiPinType._input,
    "output": KiPinType.output,
    "bidirectional": KiPinType.bidirectional,
    "tri_state": KiPinType.tri_state,
    "passive": KiPinType.passive,
    "free": KiPinType.free,
    "unspecified": KiPinType.unspecified,
    "power_in": KiPinType.power_in,
    "power_out": KiPinType.power_out,
    "open_collector": KiPinType.open_collector,
    "open_emitter": KiPinType.open_emitter,
    "no_connect": KiPinType.no_connect,
}

_PIN_STYLE_TO_ENUM = {style.name: style for style in KiPinStyle}

_GROUND_NAMES = {
    "GND",
    "AGND",
    "DGND",
    "PGND",
    "GNDA",
    "GNDD",
    "VSS",
    "VSSA",
    "VSSD",
    "VEE",
    "VNEG",
    "COM",
}

_POSITIVE_POWER_MARKERS = (
    "VCC",
    "VDD",
    "VDDA",
    "VDDD",
    "VIO",
    "VREF",
    "VBAT",
    "VBUS",
    "VIN",
    "PVIN",
    "AVDD",
    "DVDD",
    "IOVDD",
    "3V3",
    "5V",
    "1V",
    "2V",
    "V+",
    "+V",
)


CODEX_JOBS: dict[str, dict[str, Any]] = {}
CODEX_PROCESSES: dict[str, subprocess.Popen] = {}
IMPORT_JOBS: dict[str, dict[str, Any]] = {}


def _pin_type_name(pin_type: KiPinType) -> str:
    return pin_type.name[1:] if pin_type.name.startswith("_") else pin_type.name


def _actual_orientation(pin: KiSymbolPin) -> int:
    return int((180 + pin.orientation) % 360)


def _set_actual_orientation(pin: KiSymbolPin, orientation: int) -> None:
    pin.orientation = (orientation - 180) % 360


def _clean_pin_name(name: str) -> str:
    return re.sub(r"\s+", "", name or "~")


def _net_key(name: str) -> str:
    cleaned = _clean_pin_name(name).upper()
    cleaned = cleaned.replace("~{", "").replace("}", "")
    cleaned = re.sub(r"[^A-Z0-9_+\-]", "", cleaned)
    return cleaned or "PIN"


def _natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


def _expand_stack_number(number: str) -> list[str]:
    stripped = number.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return [stripped]

    result: list[str] = []
    for part in stripped[1:-1].split(","):
        part = part.strip()
        if not part:
            continue

        range_match = re.fullmatch(r"([A-Za-z]*)(\d+)-([A-Za-z]*)(\d+)", part)
        if range_match:
            p1, start, p2, end = range_match.groups()
            if p1 == p2:
                start_i = int(start)
                end_i = int(end)
                step = 1 if end_i >= start_i else -1
                width = max(len(start), len(end))
                result.extend(
                    f"{p1}{idx:0{width}d}" if width > 1 else f"{p1}{idx}"
                    for idx in range(start_i, end_i + step, step)
                )
                continue

        result.append(part)

    return result


def _format_stack_number(numbers: list[str]) -> str:
    unique = sorted({num.strip() for num in numbers if num.strip()}, key=_natural_key)
    if len(unique) == 1:
        return unique[0]
    return "[" + ",".join(unique) + "]"


def _merge_notes(*groups: list[Any] | tuple[Any, ...] | None) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for note in group or []:
            text = str(note).strip()
            if text and text not in seen:
                merged.append(text)
                seen.add(text)
    return merged


def _format_elapsed(seconds: float) -> str:
    seconds_i = max(0, int(seconds))
    minutes, remainder = divmod(seconds_i, 60)
    if minutes:
        return f"{minutes}m {remainder:02d}s"
    return f"{remainder}s"


def _codex_exec_args(schema_path: Path, last_message: Path) -> list[str]:
    args = [
        "codex",
        "--search",
        "-a",
        "never",
    ]
    profile = os.environ.get(CODEX_PROFILE_ENV, DEFAULT_CODEX_PROFILE).strip()
    if profile.lower() not in {"", "none", "off", "false", "0"}:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", profile):
            raise ValueError(
                f"{CODEX_PROFILE_ENV} must be a plain Codex profile name, not a prompt."
            )
        args.extend(["-p", profile])
    args.extend(
        [
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(last_message),
            "-",
        ]
    )
    return args


def _is_ground(pin_name: str) -> bool:
    key = _net_key(pin_name)
    return key in _GROUND_NAMES or any(key.endswith(f"_{name}") for name in _GROUND_NAMES)


def _is_positive_power(pin_name: str) -> bool:
    key = _net_key(pin_name)
    if key in _GROUND_NAMES:
        return False
    if key.startswith(_POSITIVE_POWER_MARKERS) or key.endswith(_POSITIVE_POWER_MARKERS):
        return True
    return any(f"_{marker}" in key for marker in _POSITIVE_POWER_MARKERS)


def _clean_metadata_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _default_fp_filter(library: str, footprint_name: str) -> str:
    cleaned = _clean_metadata_text(footprint_name)
    if not cleaned:
        return ""
    pattern = re.sub(r"[-_\s]+", "*", cleaned)
    pattern = re.sub(r"[^A-Za-z0-9.+?*:]+", "*", pattern)
    pattern = re.sub(r"\*+", "*", pattern).strip("*")
    if pattern:
        pattern += "*"

    library = _clean_metadata_text(library)
    if library and pattern:
        return f"{library}:{pattern}"
    return pattern


def _footprint_name_from_field(footprint_field: str) -> str:
    cleaned = _clean_metadata_text(footprint_field)
    return cleaned.split(":", 1)[-1] if ":" in cleaned else cleaned


def _asset_manifest_for_payload(
    payload: dict[str, Any],
    *,
    bundle_names: set[str] | None = None,
    model_refs: list[str] | None = None,
) -> dict[str, Any]:
    library = _clean_metadata_text(payload.get("library") or DEFAULT_LIBRARY) or DEFAULT_LIBRARY
    symbol_info = dict(payload.get("symbol", {}).get("info") or {})
    footprint = dict(payload.get("footprint") or {})
    footprint_name = _footprint_name_from_field(str(symbol_info.get("package") or "")) or _clean_metadata_text(
        footprint.get("name")
    )
    model_name = _clean_metadata_text(footprint.get("model_name"))
    has_model = bool(footprint.get("has_3d_model") or model_name)
    model_dir = f"{library}.3dshapes" if has_model else ""
    model_files = (
        sorted(name for name in bundle_names if f"{library}.3dshapes/" in name)
        if bundle_names is not None
        else []
    )
    refs = sorted(set(model_refs or []))
    if model_files:
        model_status = "included"
    elif has_model and bundle_names is not None:
        model_status = "missing"
    elif has_model:
        model_status = "pending"
    else:
        model_status = "none"

    return {
        "library": library,
        "symbol_file": f"{library}.kicad_sym",
        "footprint_file": f"{library}.pretty/{footprint_name}.kicad_mod" if footprint_name else "",
        "model_dir": model_dir,
        "model_name": model_name,
        "model_status": model_status,
        "model_files": model_files,
        "model_refs": refs,
        "zip_file_count": len(bundle_names or []),
    }


def _is_library_payload(payload: dict[str, Any]) -> bool:
    return isinstance(payload.get("symbols"), list)


def _library_name(payload: dict[str, Any]) -> str:
    return _clean_metadata_text(payload.get("library") or DEFAULT_LIBRARY) or DEFAULT_LIBRARY


def _library_symbol_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not _is_library_payload(payload):
        return [payload]

    library = _library_name(payload)
    result: list[dict[str, Any]] = []
    for item in payload.get("symbols") or []:
        if not isinstance(item, dict):
            continue
        symbol_payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
        if not isinstance(symbol_payload, dict) or not symbol_payload.get("symbol"):
            continue
        cloned = deepcopy(symbol_payload)
        cloned["library"] = library
        result.append(cloned)
    return result


def _active_unit_index(payload: dict[str, Any]) -> int:
    symbol_data = payload.get("symbol", {})
    units = symbol_data.get("units")
    if not isinstance(units, list) or not units:
        return 0
    try:
        index = int(symbol_data.get("active_unit") or 0)
    except (TypeError, ValueError):
        index = 0
    return max(0, min(index, len(units) - 1))


def _sync_active_unit_payload(payload: dict[str, Any]) -> None:
    symbol_data = payload.get("symbol", {})
    units = symbol_data.get("units")
    if not isinstance(units, list) or not units:
        return
    index = _active_unit_index(payload)
    units[index]["pins"] = deepcopy(symbol_data.get("pins") or [])


def _keywords_from_overview(overview: dict[str, Any], footprint_name: str) -> str:
    raw = " ".join(
        _clean_metadata_text(value)
        for value in (
            overview.get("mpn"),
            overview.get("manufacturer"),
            overview.get("package"),
            footprint_name,
            overview.get("lcsc_id"),
        )
        if value
    )
    tokens = re.findall(r"[A-Za-z0-9.+-]+", raw)
    unique: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        key = token.casefold()
        if key not in seen:
            unique.append(token)
            seen.add(key)
    return " ".join(unique[:16])


def _description_from_overview(
    overview: dict[str, Any],
    footprint_name: str,
    existing_description: str,
) -> str:
    description = _clean_metadata_text(
        overview.get("description") or overview.get("name") or existing_description
    )
    package = _clean_metadata_text(overview.get("package") or footprint_name)
    if package and package not in description:
        description = f"{description}, {package}" if description else package
    return description


def _classify_side(pin: KiSymbolPin) -> str:
    pin_type = _pin_type_name(pin.type)
    name = pin.name

    if _is_ground(name) or pin_type == "no_connect":
        return "bottom"
    if pin_type == "power_in" and _is_positive_power(name):
        return "top"
    if pin_type == "power_out":
        return "right"
    if pin_type in {"output", "tri_state", "open_collector", "open_emitter"}:
        return "right"
    if pin_type in {"input", "bidirectional"}:
        return "left"
    if pin_type == "power_in":
        return "top"
    return "left"


def _centered_positions(count: int, spacing: float = PIN_SPACING) -> list[float]:
    if count <= 0:
        return []
    if count % 2:
        middle = count // 2
        return [round((middle - idx) * spacing, 2) for idx in range(count)]
    half = count // 2
    return [
        round((half - idx) * spacing if idx < half else -(idx - half + 1) * spacing, 2)
        for idx in range(count)
    ]


def _centered_x_positions(count: int, spacing: float = PIN_SPACING) -> list[float]:
    if count <= 0:
        return []
    if count % 2:
        middle = count // 2
        return [round((idx - middle) * spacing, 2) for idx in range(count)]
    half = count // 2
    return [
        round(-(half - idx) * spacing if idx < half else (idx - half + 1) * spacing, 2)
        for idx in range(count)
    ]


def _set_pin_side_position(
    pin: KiSymbolPin,
    side: str,
    offset: float,
    half_width: float,
    half_height: float,
) -> None:
    pin.length = PIN_LENGTH

    if side == "left":
        pin.pos_x = round(-half_width - PIN_LENGTH, 2)
        pin.pos_y = offset
        _set_actual_orientation(pin, 0)
    elif side == "right":
        pin.pos_x = round(half_width + PIN_LENGTH, 2)
        pin.pos_y = offset
        _set_actual_orientation(pin, 180)
    elif side == "top":
        pin.pos_x = offset
        pin.pos_y = round(half_height + PIN_LENGTH, 2)
        _set_actual_orientation(pin, 270)
    else:
        pin.pos_x = offset
        pin.pos_y = round(-half_height - PIN_LENGTH, 2)
        _set_actual_orientation(pin, 90)


def _stack_logical_pins(pins: list[KiSymbolPin]) -> tuple[list[KiSymbolPin], list[str]]:
    grouped: dict[tuple[str, str, str], list[KiSymbolPin]] = {}
    for pin in pins:
        pin.name = _clean_pin_name(pin.name)
        if (_is_ground(pin.name) or _is_positive_power(pin.name)) and pin.type not in {
            KiPinType.power_out,
            KiPinType.no_connect,
        }:
            pin.type = KiPinType.power_in
        key = (_net_key(pin.name), _pin_type_name(pin.type), pin.style.name)
        if pin.type == KiPinType.no_connect:
            key = (_net_key(pin.name) + "_" + pin.number, _pin_type_name(pin.type), pin.style.name)
        grouped.setdefault(key, []).append(pin)

    stacked: list[KiSymbolPin] = []
    notes: list[str] = []

    for group in grouped.values():
        master = deepcopy(group[0])
        master.hidden = False
        numbers: list[str] = []
        for pin in group:
            numbers.extend(_expand_stack_number(pin.number))
        master.number = _format_stack_number(numbers)
        if len(group) > 1:
            notes.append(
                f"Stacked {len(group)} {master.name} pins as native KiCad number {master.number}."
            )
        stacked.append(master)

    return stacked, notes


def clean_symbol(ki_symbol: KiSymbol) -> tuple[KiSymbol, list[str]]:
    cleaned = deepcopy(ki_symbol)
    cleaned.info.name = sanitize_fields(cleaned.info.name or cleaned.info.mpn or "Imported_Part")
    cleaned.info.prefix = (cleaned.info.prefix or "U").replace("?", "")

    cleaned.pins, notes = _stack_logical_pins(cleaned.pins)

    side_groups: dict[str, list[KiSymbolPin]] = {
        "left": [],
        "right": [],
        "top": [],
        "bottom": [],
    }
    for pin in cleaned.pins:
        side_groups[_classify_side(pin)].append(pin)

    for pins in side_groups.values():
        pins.sort(key=lambda pin: (_net_key(pin.name), _natural_key(pin.number)))

    max_vertical = max(len(side_groups["left"]), len(side_groups["right"]), 1)
    max_horizontal = max(len(side_groups["top"]), len(side_groups["bottom"]), 1)
    body_height = max(7.62, (max_vertical + 1) * PIN_SPACING)
    body_width = max(10.16, (max_horizontal + 1) * PIN_SPACING)
    half_height = round(round(body_height / PIN_SPACING / 2) * PIN_SPACING, 2)
    half_width = round(round(body_width / PIN_SPACING / 2) * PIN_SPACING, 2)
    half_height = max(3.81, half_height)
    half_width = max(5.08, half_width)

    for side in ("left", "right"):
        for pin, y in zip(side_groups[side], _centered_positions(len(side_groups[side]))):
            _set_pin_side_position(pin, side, y, half_width, half_height)

    for side in ("top", "bottom"):
        for pin, x in zip(side_groups[side], _centered_x_positions(len(side_groups[side]))):
            _set_pin_side_position(pin, side, x, half_width, half_height)

    cleaned.pins = (
        side_groups["top"]
        + side_groups["left"]
        + side_groups["right"]
        + side_groups["bottom"]
    )
    cleaned.rectangles = [
        KiSymbolRectangle(
            pos_x0=-half_width,
            pos_y0=half_height,
            pos_x1=half_width,
            pos_y1=-half_height,
        )
    ]
    cleaned.circles = []
    cleaned.arcs = []
    cleaned.polygons = []
    cleaned.beziers = []
    cleaned.texts = []

    notes.append("Placed positive power pins on top, ground/negative pins on bottom, inputs left, and outputs right.")
    notes.append("Rebuilt body as a centered KLC-style filled rectangle on the 100 mil grid.")
    return cleaned, notes


def _pins_to_payload(pins: list[KiSymbolPin]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for idx, pin in enumerate(pins):
        result.append(
            {
                "id": f"pin-{idx}",
                "name": pin.name,
                "number": pin.number,
                "stacked_numbers": _expand_stack_number(pin.number),
                "type": _pin_type_name(pin.type),
                "style": pin.style.name,
                "length": pin.length,
                "x": pin.pos_x,
                "y": pin.pos_y,
                "orientation": _actual_orientation(pin),
                "side": _classify_side(pin),
            }
        )
    return result


def _unit_to_payload(
    name: str,
    ki_symbol: KiSymbol,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "pins": _pins_to_payload(ki_symbol.pins),
        "notes": list(notes or []),
    }


def _symbol_to_payload(
    ki_symbol: KiSymbol,
    *,
    lcsc_id: str,
    overview: dict[str, Any],
    footprint: dict[str, Any],
    notes: list[str],
    codex_job_id: str | None = None,
    units: list[tuple[str, KiSymbol, list[str]]] | None = None,
) -> dict[str, Any]:
    info = asdict(ki_symbol.info)
    custom_fields = info.pop("custom_fields", {})

    unit_payloads = [
        _unit_to_payload(name, unit_symbol, unit_notes)
        for name, unit_symbol, unit_notes in units or []
    ]
    pins = (
        deepcopy(unit_payloads[0]["pins"])
        if unit_payloads
        else _pins_to_payload(ki_symbol.pins)
    )
    symbol_notes = notes
    if len(unit_payloads) > 1:
        symbol_notes = _merge_notes(
            notes,
            [f"Imported {len(unit_payloads)} EasyEDA symbol units."],
        )

    symbol_payload: dict[str, Any] = {
        "info": info,
        "custom_fields": custom_fields,
        "pins": pins,
        "notes": symbol_notes,
    }
    if len(unit_payloads) > 1:
        symbol_payload["units"] = unit_payloads
        symbol_payload["active_unit"] = 0

    return {
        "lcsc_id": lcsc_id,
        "library": DEFAULT_LIBRARY,
        "symbol": symbol_payload,
        "footprint": footprint,
        "overview": overview,
        "assets": _asset_manifest_for_payload(
            {
                "library": DEFAULT_LIBRARY,
                "symbol": {"info": info},
                "footprint": footprint,
            }
        ),
        "codex_job_id": codex_job_id,
        "pin_type_values": PIN_TYPE_VALUES,
        "pin_style_values": PIN_STYLE_VALUES,
    }


def _payload_to_symbol(payload: dict[str, Any]) -> KiSymbol:
    symbol_data = payload.get("symbol", {})
    info_data = symbol_data.get("info", {})
    custom_fields = symbol_data.get("custom_fields", {})

    info = KiSymbolInfo(
        name=sanitize_fields(str(info_data.get("name") or "Imported_Part")),
        prefix=str(info_data.get("prefix") or "U").replace("?", ""),
        package=str(info_data.get("package") or ""),
        manufacturer=str(info_data.get("manufacturer") or ""),
        datasheet=str(info_data.get("datasheet") or ""),
        lcsc_id=str(info_data.get("lcsc_id") or payload.get("lcsc_id") or ""),
        mpn=str(info_data.get("mpn") or ""),
        keywords=str(info_data.get("keywords") or ""),
        description=str(info_data.get("description") or ""),
        fp_filters=str(info_data.get("fp_filters") or ""),
        custom_fields={
            str(key): str(value) for key, value in dict(custom_fields or {}).items()
        },
    )

    pins: list[KiSymbolPin] = []
    for item in symbol_data.get("pins", []):
        pin_type = _PIN_TYPE_TO_ENUM.get(str(item.get("type")), KiPinType.unspecified)
        pin_style = _PIN_STYLE_TO_ENUM.get(str(item.get("style")), KiPinStyle.line)
        pin = KiSymbolPin(
            name=_clean_pin_name(str(item.get("name") or "~")),
            number=str(item.get("number") or ""),
            style=pin_style,
            length=float(item.get("length") or PIN_LENGTH),
            type=pin_type,
            orientation=0,
            pos_x=float(item.get("x") or 0),
            pos_y=float(item.get("y") or 0),
        )
        _set_actual_orientation(pin, int(float(item.get("orientation") or 0)))
        pins.append(pin)

    symbol = KiSymbol(info=info, pins=pins)
    return symbol


def payload_to_export_symbol(payload: dict[str, Any], clean_layout: bool = False) -> KiSymbol:
    symbol = _payload_to_symbol(payload)
    if clean_layout:
        symbol, _ = clean_symbol(symbol)
    else:
        xs = [pin.pos_x for pin in symbol.pins]
        ys = [pin.pos_y for pin in symbol.pins]
        half_width = max(5.08, round((max(abs(x) for x in xs) - PIN_LENGTH) if xs else 5.08, 2))
        half_height = max(3.81, round((max(abs(y) for y in ys) - PIN_LENGTH) if ys else 3.81, 2))
        symbol.rectangles = [
            KiSymbolRectangle(
                pos_x0=-half_width,
                pos_y0=half_height,
                pos_x1=half_width,
                pos_y1=-half_height,
            )
        ]
    return symbol


def _payload_to_unit_symbols(payload: dict[str, Any]) -> list[KiSymbol]:
    symbol_data = payload.get("symbol", {})
    units = symbol_data.get("units")
    if not isinstance(units, list) or not units:
        return [payload_to_export_symbol(payload)]

    active_index = _active_unit_index(payload)
    result: list[KiSymbol] = []
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            continue
        unit_payload = deepcopy(payload)
        unit_pins = (
            symbol_data.get("pins")
            if index == active_index
            else unit.get("pins")
        )
        unit_payload.setdefault("symbol", {})["pins"] = deepcopy(unit_pins or [])
        result.append(payload_to_export_symbol(unit_payload))
    return result or [payload_to_export_symbol(payload)]


def _symbol_attrs() -> str:
    if KICAD_VERSION >= 20241209:
        return "(exclude_from_sim no)\n    (in_bom yes)\n    (on_board yes)"
    return "(in_bom yes)\n    (on_board yes)"


def _export_unit_block(symbol_name: str, unit_index: int, symbol: KiSymbol) -> str:
    export_data = symbol.export_handler(version=KICAD_VERSION)
    export_data.pop("info", None)
    pins = "".join(export_data.pop("pins"))
    graphic_items = "".join(
        "".join(items) for items in export_data.values() if isinstance(items, list)
    )
    body = textwrap.indent(textwrap.dedent(graphic_items + pins), "      ")
    return f'    (symbol "{symbol_name}_{unit_index}_1"\n{body}\n    )'


def _export_multi_unit_symbol(payload: dict[str, Any], unit_symbols: list[KiSymbol]) -> str:
    symbol_name = sanitize_fields(
        str(payload.get("symbol", {}).get("info", {}).get("name") or "Imported_Part")
    )
    info = deepcopy(unit_symbols[0].info)
    all_pins = [pin for unit_symbol in unit_symbols for pin in unit_symbol.pins]
    info.y_low = min((pin.pos_y for pin in all_pins), default=0)
    info.y_high = max((pin.pos_y for pin in all_pins), default=0)
    properties = textwrap.indent(
        textwrap.dedent("".join(info.export(version=KICAD_VERSION))),
        "    ",
    )
    units = "\n".join(
        _export_unit_block(symbol_name, index, unit_symbol)
        for index, unit_symbol in enumerate(unit_symbols, start=1)
    )
    component = f"""  (symbol "{symbol_name}"
    {_symbol_attrs()}
{properties}
{units}
  )"""
    return re.sub(r"\n\s*\n", "\n", component, flags=re.MULTILINE)


def export_symbol_component(payload: dict[str, Any]) -> str:
    unit_symbols = _payload_to_unit_symbols(payload)
    if len(unit_symbols) == 1:
        return unit_symbols[0].export(version=KICAD_VERSION).rstrip()
    return _export_multi_unit_symbol(payload, unit_symbols).rstrip()


def export_symbol_library(payload: dict[str, Any]) -> str:
    symbol_payloads = _library_symbol_payloads(payload)
    if not symbol_payloads:
        raise ValueError("Library export requires at least one symbol.")
    library = _library_name(payload)
    components = []
    for symbol_payload in symbol_payloads:
        symbol_payload = deepcopy(symbol_payload)
        symbol_payload["library"] = library
        _sync_active_unit_payload(symbol_payload)
        components.append(export_symbol_component(symbol_payload))
    component_text = "\n".join(components)
    return (
        "(kicad_symbol_lib\n"
        f"  (version {KICAD_VERSION})\n"
        f'  (generator "{GENERATOR}")\n'
        f"  (generator_version \"1.0\")\n"
        f"{component_text}\n"
        ")\n"
    )


def validate_symbol_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if _is_library_payload(payload):
        return validate_library_payload(payload)

    checks = _payload_klc_checks(payload)
    symbol_name = sanitize_fields(
        str(payload.get("symbol", {}).get("info", {}).get("name") or "Imported_Part")
    )

    try:
        content = export_symbol_library(payload)
    except Exception as exc:
        checks.append(
            {
                "level": "error",
                "message": f"Could not render KiCad symbol library: {exc}",
            }
        )
        return _validation_result(checks)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            library_path = tmp_path / "symbol.kicad_sym"
            svg_dir = tmp_path / "svg"
            svg_dir.mkdir()
            library_path.write_text(content, encoding="utf-8")

            upgrade = subprocess.run(
                [
                    "kicad-cli",
                    "sym",
                    "upgrade",
                    "--force",
                    str(library_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if upgrade.returncode == 0:
                checks.append(
                    {
                        "level": "pass",
                        "message": "KiCad accepted the symbol library file.",
                    }
                )
            else:
                checks.append(
                    {
                        "level": "error",
                        "message": _command_error("KiCad symbol upgrade failed", upgrade),
                    }
                )
                return _validation_result(checks)

            export = subprocess.run(
                [
                    "kicad-cli",
                    "sym",
                    "export",
                    "svg",
                    "--symbol",
                    symbol_name,
                    "--output",
                    str(svg_dir),
                    str(library_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            svg_files = sorted(svg_dir.glob("*.svg"))
            svg = ""
            if export.returncode == 0 and svg_files:
                svg = svg_files[0].read_text(encoding="utf-8")
                checks.append(
                    {
                        "level": "pass",
                        "message": f"KiCad rendered SVG preview for {symbol_name}.",
                    }
                )
            else:
                checks.append(
                    {
                        "level": "error",
                        "message": _command_error("KiCad SVG export failed", export),
                    }
                )
    except FileNotFoundError:
        checks.append(
            {
                "level": "error",
                "message": "kicad-cli was not found on PATH.",
            }
        )
    except subprocess.TimeoutExpired as exc:
        checks.append(
            {
                "level": "error",
                "message": f"KiCad validation timed out: {exc}",
            }
        )

    footprint_svg = ""
    assets: dict[str, Any] = {}
    try:
        bundle_result = _validate_bundle_assets(payload)
        checks.extend(bundle_result["checks"])
        footprint_svg = str(bundle_result.get("footprint_svg") or "")
        assets = dict(bundle_result.get("assets") or {})
    except Exception as exc:
        checks.append(
            {
                "level": "error",
                "message": f"Asset ZIP validation failed: {exc}",
            }
        )

    result = _validation_result(checks)
    if "svg" in locals() and svg:
        result["svg"] = svg
    if footprint_svg:
        result["footprint_svg"] = footprint_svg
    if assets:
        result["assets"] = assets
    return result


def validate_library_payload(payload: dict[str, Any]) -> dict[str, Any]:
    symbol_payloads = _library_symbol_payloads(payload)
    checks: list[dict[str, str]] = []
    if not symbol_payloads:
        return _validation_result(
            [{"level": "error", "message": "Library has no symbols to validate."}]
        )

    for symbol_payload in symbol_payloads:
        symbol_name = sanitize_fields(
            str(symbol_payload.get("symbol", {}).get("info", {}).get("name") or "Imported_Part")
        )
        for check in _payload_klc_checks(symbol_payload):
            checks.append(
                {
                    "level": check.get("level", "warn"),
                    "message": f"{symbol_name}: {check.get('message', '')}",
                }
            )

    first_symbol_name = sanitize_fields(
        str(symbol_payloads[0].get("symbol", {}).get("info", {}).get("name") or "Imported_Part")
    )

    try:
        content = export_symbol_library(payload)
    except Exception as exc:
        checks.append(
            {
                "level": "error",
                "message": f"Could not render KiCad symbol library: {exc}",
            }
        )
        return _validation_result(checks)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            library_path = tmp_path / "symbol.kicad_sym"
            svg_dir = tmp_path / "svg"
            svg_dir.mkdir()
            library_path.write_text(content, encoding="utf-8")

            upgrade = subprocess.run(
                [
                    "kicad-cli",
                    "sym",
                    "upgrade",
                    "--force",
                    str(library_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if upgrade.returncode == 0:
                checks.append(
                    {
                        "level": "pass",
                        "message": "KiCad accepted the combined symbol library file.",
                    }
                )
            else:
                checks.append(
                    {
                        "level": "error",
                        "message": _command_error("KiCad symbol upgrade failed", upgrade),
                    }
                )
                return _validation_result(checks)

            export = subprocess.run(
                [
                    "kicad-cli",
                    "sym",
                    "export",
                    "svg",
                    "--symbol",
                    first_symbol_name,
                    "--output",
                    str(svg_dir),
                    str(library_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            svg_files = sorted(svg_dir.glob("*.svg"))
            svg = ""
            if export.returncode == 0 and svg_files:
                svg = svg_files[0].read_text(encoding="utf-8")
                checks.append(
                    {
                        "level": "pass",
                        "message": f"KiCad rendered SVG preview for {first_symbol_name}.",
                    }
                )
            else:
                checks.append(
                    {
                        "level": "error",
                        "message": _command_error("KiCad SVG export failed", export),
                    }
                )
    except FileNotFoundError:
        checks.append(
            {
                "level": "error",
                "message": "kicad-cli was not found on PATH.",
            }
        )
    except subprocess.TimeoutExpired as exc:
        checks.append(
            {
                "level": "error",
                "message": f"KiCad validation timed out: {exc}",
            }
        )

    footprint_svg = ""
    assets: dict[str, Any] = {}
    try:
        bundle_result = _validate_bundle_assets(payload)
        checks.extend(bundle_result["checks"])
        footprint_svg = str(bundle_result.get("footprint_svg") or "")
        assets = dict(bundle_result.get("assets") or {})
    except Exception as exc:
        checks.append(
            {
                "level": "error",
                "message": f"Asset ZIP validation failed: {exc}",
            }
        )

    result = _validation_result(checks)
    if "svg" in locals() and svg:
        result["svg"] = svg
    if footprint_svg:
        result["footprint_svg"] = footprint_svg
    if assets:
        result["assets"] = assets
    return result


def _validation_result(checks: list[dict[str, str]]) -> dict[str, Any]:
    has_error = any(check.get("level") == "error" for check in checks)
    has_warning = any(check.get("level") == "warn" for check in checks)
    if has_error:
        status = "error"
        message = "Validation failed"
    elif has_warning:
        status = "warn"
        message = "Validation passed with warnings"
    else:
        status = "ok"
        message = "Validation passed"
    return {"status": status, "message": message, "checks": checks}


def _command_error(prefix: str, completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or completed.stdout or "").strip()
    return f"{prefix}: {detail}" if detail else prefix


def _validate_bundle_assets(payload: dict[str, Any]) -> dict[str, Any]:
    if _is_library_payload(payload):
        return _validate_library_bundle_assets(payload)

    checks: list[dict[str, str]] = []
    footprint_svg = ""
    model_refs: list[str] = []
    bundle = export_bundle_zip(payload)
    library = str(payload.get("library") or DEFAULT_LIBRARY)
    expected_symbol = f"{library}.kicad_sym"
    footprint_field = str(payload.get("symbol", {}).get("info", {}).get("package") or "")
    expected_footprint_name = _footprint_name_from_field(footprint_field)
    expected_footprint = (
        f"{library}.pretty/{expected_footprint_name}.kicad_mod"
        if expected_footprint_name
        else ""
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            names = set(archive.namelist())
            archive.extractall(root)

        if expected_symbol in names:
            checks.append({"level": "pass", "message": "Asset ZIP contains the symbol library."})
        else:
            checks.append(
                {
                    "level": "error",
                    "message": f"Asset ZIP is missing {expected_symbol}.",
                }
            )

        footprint_names = sorted(name for name in names if name.endswith(".kicad_mod"))
        footprint_rel = expected_footprint if expected_footprint in names else ""
        if footprint_rel:
            checks.append(
                {
                    "level": "pass",
                    "message": f"Asset ZIP contains footprint {expected_footprint_name}.",
                }
            )
        elif expected_footprint:
            checks.append(
                {
                    "level": "error",
                    "message": f"Symbol footprint field points to missing {expected_footprint}.",
                }
            )
        elif len(footprint_names) == 1:
            footprint_rel = footprint_names[0]
            expected_footprint_name = Path(footprint_rel).stem
            checks.append(
                {
                    "level": "warn",
                    "message": f"No symbol footprint field; validating {footprint_rel}.",
                }
            )
        else:
            checks.append({"level": "error", "message": "Asset ZIP contains no footprint."})

        if footprint_rel:
            footprint_path = root / footprint_rel
            model_refs = _footprint_model_refs(footprint_path.read_text(encoding="utf-8"))
            missing_models: list[str] = []
            for model_ref in model_refs:
                model_path = _model_ref_to_bundle_path(model_ref)
                if model_path and model_path not in names:
                    missing_models.append(model_ref)
            if missing_models:
                checks.append(
                    {
                        "level": "error",
                        "message": "Footprint references missing 3D model files: "
                        + ", ".join(missing_models)
                        + ".",
                    }
                )
            elif model_refs:
                checks.append(
                    {
                        "level": "pass",
                        "message": "Footprint 3D model references resolve inside the asset ZIP.",
                    }
                )
            else:
                checks.append(
                    {
                        "level": "pass",
                        "message": "Footprint has no 3D model reference to resolve.",
                    }
                )

            pretty_dir = root / f"{library}.pretty"
            upgrade = subprocess.run(
                [
                    "kicad-cli",
                    "fp",
                    "upgrade",
                    "--force",
                    str(pretty_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if upgrade.returncode == 0:
                checks.append({"level": "pass", "message": "KiCad accepted the footprint library."})
            else:
                checks.append(
                    {
                        "level": "error",
                        "message": _command_error("KiCad footprint upgrade failed", upgrade),
                    }
                )
                return {
                    "checks": checks,
                    "footprint_svg": "",
                    "assets": _asset_manifest_for_payload(
                        payload,
                        bundle_names=names,
                        model_refs=model_refs,
                    ),
                }

            svg_dir = root / "footprint_svg"
            svg_dir.mkdir()
            export = subprocess.run(
                [
                    "kicad-cli",
                    "fp",
                    "export",
                    "svg",
                    "--fp",
                    expected_footprint_name,
                    "--output",
                    str(svg_dir),
                    str(pretty_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            svg_files = sorted(svg_dir.glob("*.svg"))
            if export.returncode == 0 and svg_files:
                footprint_svg = svg_files[0].read_text(encoding="utf-8")
                checks.append(
                    {
                        "level": "pass",
                        "message": f"KiCad rendered footprint SVG for {expected_footprint_name}.",
                    }
                )
            else:
                checks.append(
                    {
                        "level": "error",
                        "message": _command_error("KiCad footprint SVG export failed", export),
                    }
                )

    return {
        "checks": checks,
        "footprint_svg": footprint_svg,
        "assets": _asset_manifest_for_payload(
            payload,
            bundle_names=names if "names" in locals() else set(),
            model_refs=model_refs,
        ),
    }


def _validate_library_bundle_assets(payload: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    footprint_svg = ""
    model_refs: list[str] = []
    symbol_payloads = _library_symbol_payloads(payload)
    library = _library_name(payload)
    expected_symbol = f"{library}.kicad_sym"
    bundle = export_bundle_zip(payload)

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            names = set(archive.namelist())
            archive.extractall(root)

        if expected_symbol in names:
            checks.append({"level": "pass", "message": "Asset ZIP contains the combined symbol library."})
        else:
            checks.append(
                {
                    "level": "error",
                    "message": f"Asset ZIP is missing {expected_symbol}.",
                }
            )

        expected_footprints: list[tuple[str, str]] = []
        for symbol_payload in symbol_payloads:
            symbol_name = sanitize_fields(
                str(symbol_payload.get("symbol", {}).get("info", {}).get("name") or "Imported_Part")
            )
            footprint_field = str(symbol_payload.get("symbol", {}).get("info", {}).get("package") or "")
            footprint_name = _footprint_name_from_field(footprint_field)
            if footprint_name:
                expected_footprints.append(
                    (symbol_name, f"{library}.pretty/{footprint_name}.kicad_mod")
                )

        for symbol_name, footprint_rel in expected_footprints:
            if footprint_rel in names:
                checks.append(
                    {
                        "level": "pass",
                        "message": f"{symbol_name}: Asset ZIP contains {footprint_rel}.",
                    }
                )
            else:
                checks.append(
                    {
                        "level": "error",
                        "message": f"{symbol_name}: Symbol footprint field points to missing {footprint_rel}.",
                    }
                )

        pretty_dir = root / f"{library}.pretty"
        footprint_paths = sorted(pretty_dir.glob("*.kicad_mod")) if pretty_dir.exists() else []
        if not footprint_paths:
            checks.append({"level": "error", "message": "Asset ZIP contains no footprints."})
        else:
            missing_models: list[str] = []
            for footprint_path in footprint_paths:
                refs = _footprint_model_refs(footprint_path.read_text(encoding="utf-8"))
                model_refs.extend(refs)
                for model_ref in refs:
                    model_path = _model_ref_to_bundle_path(model_ref)
                    if model_path and model_path not in names:
                        missing_models.append(model_ref)
            if missing_models:
                checks.append(
                    {
                        "level": "error",
                        "message": "Footprints reference missing 3D model files: "
                        + ", ".join(sorted(set(missing_models)))
                        + ".",
                    }
                )
            else:
                checks.append(
                    {
                        "level": "pass",
                        "message": "Footprint 3D model references resolve inside the asset ZIP.",
                    }
                )

            upgrade = subprocess.run(
                [
                    "kicad-cli",
                    "fp",
                    "upgrade",
                    "--force",
                    str(pretty_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if upgrade.returncode == 0:
                checks.append({"level": "pass", "message": "KiCad accepted the footprint library."})
            else:
                checks.append(
                    {
                        "level": "error",
                        "message": _command_error("KiCad footprint upgrade failed", upgrade),
                    }
                )
                return {
                    "checks": checks,
                    "footprint_svg": "",
                    "assets": {
                        "library": library,
                        "symbol_file": expected_symbol,
                        "zip_file_count": len(names),
                        "model_files": sorted(name for name in names if f"{library}.3dshapes/" in name),
                        "model_refs": sorted(set(model_refs)),
                        "model_status": "included" if model_refs else "none",
                    },
                }

            first_footprint = footprint_paths[0]
            svg_dir = root / "footprint_svg"
            svg_dir.mkdir()
            export = subprocess.run(
                [
                    "kicad-cli",
                    "fp",
                    "export",
                    "svg",
                    "--fp",
                    first_footprint.stem,
                    "--output",
                    str(svg_dir),
                    str(pretty_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            svg_files = sorted(svg_dir.glob("*.svg"))
            if export.returncode == 0 and svg_files:
                footprint_svg = svg_files[0].read_text(encoding="utf-8")
                checks.append(
                    {
                        "level": "pass",
                        "message": f"KiCad rendered footprint SVG for {first_footprint.stem}.",
                    }
                )
            else:
                checks.append(
                    {
                        "level": "error",
                        "message": _command_error("KiCad footprint SVG export failed", export),
                    }
                )

    return {
        "checks": checks,
        "footprint_svg": footprint_svg,
        "assets": {
            "library": library,
            "symbol_file": expected_symbol,
            "zip_file_count": len(names) if "names" in locals() else 0,
            "model_files": sorted(name for name in names if f"{library}.3dshapes/" in name) if "names" in locals() else [],
            "model_refs": sorted(set(model_refs)),
            "model_status": "included" if model_refs else "none",
        },
    }


def _footprint_model_refs(footprint_text: str) -> list[str]:
    refs: list[str] = []
    for match in re.finditer(r"\(model\s+(\"([^\"]+)\"|([^\s)]+))", footprint_text):
        refs.append(match.group(2) or match.group(3) or "")
    return [ref for ref in refs if ref]


def _model_ref_to_bundle_path(model_ref: str) -> str:
    cleaned = model_ref.replace("\\", "/")
    for prefix in ("${KIPRJMOD}/", "$KIPRJMOD/"):
        if cleaned.startswith(prefix):
            return cleaned[len(prefix) :]
    return cleaned.lstrip("/") if not cleaned.startswith("$") else ""


def _payload_klc_checks(payload: dict[str, Any]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    unit_symbols = _payload_to_unit_symbols(payload)
    pins = [pin for symbol in unit_symbols for pin in symbol.pins]

    if not pins:
        checks.append({"level": "error", "message": "Symbol has no pins."})
        return checks

    empty_units = [
        str(index)
        for index, symbol in enumerate(unit_symbols, start=1)
        if not symbol.pins
    ]
    if empty_units:
        checks.append(
            {
                "level": "error",
                "message": f"Symbol units have no pins: {', '.join(empty_units)}.",
            }
        )
    elif len(unit_symbols) > 1:
        checks.append(
            {
                "level": "pass",
                "message": f"Multi-unit symbol contains {len(unit_symbols)} populated units.",
            }
        )

    expanded_numbers: dict[str, str] = {}
    duplicates: set[str] = set()
    for pin in pins:
        for number in _expand_stack_number(pin.number):
            if number in expanded_numbers:
                duplicates.add(number)
            expanded_numbers[number] = pin.number

    if duplicates:
        duplicate_list = ", ".join(sorted(duplicates, key=_natural_key))
        checks.append(
            {
                "level": "error",
                "message": f"Duplicate expanded pin numbers: {duplicate_list}.",
            }
        )
    else:
        checks.append({"level": "pass", "message": "Expanded pin numbers are unique."})

    lengths = {round(pin.length, 2) for pin in pins}
    if len(lengths) == 1 and PIN_LENGTH <= next(iter(lengths)) <= 7.62:
        checks.append({"level": "pass", "message": "Pins use one KLC-compliant length."})
    else:
        checks.append(
            {
                "level": "warn",
                "message": "KLC expects all pins to share one 100-300 mil length.",
            }
        )

    off_grid = [
        pin.number
        for pin in pins
        if not _on_grid(pin.pos_x, PIN_SPACING) or not _on_grid(pin.pos_y, PIN_SPACING)
    ]
    if off_grid:
        checks.append(
            {
                "level": "warn",
                "message": f"Pin origins are off the 100 mil grid: {', '.join(off_grid)}.",
            }
        )
    else:
        checks.append({"level": "pass", "message": "Pin origins are on the 100 mil grid."})

    unspecified = [
        pin.number for pin in pins if pin.type in {KiPinType.unspecified, KiPinType.free}
    ]
    if unspecified:
        checks.append(
            {
                "level": "warn",
                "message": (
                    "Pins still need datasheet electrical-type review: "
                    + ", ".join(unspecified)
                    + "."
                ),
            }
        )
    else:
        checks.append({"level": "pass", "message": "No pins use unspecified/free type."})

    no_connect_stacks = [
        pin.number
        for pin in pins
        if pin.type == KiPinType.no_connect and len(_expand_stack_number(pin.number)) > 1
    ]
    if no_connect_stacks:
        checks.append(
            {
                "level": "error",
                "message": "No-connect pins cannot be native-stacked: "
                + ", ".join(no_connect_stacks)
                + ".",
            }
        )
    else:
        checks.append({"level": "pass", "message": "No no-connect pin stacks found."})

    return checks


def _on_grid(value: float, spacing: float) -> bool:
    return abs((value / spacing) - round(value / spacing)) < 0.01


def start_import_job(lcsc_id: str, run_codex: bool = True) -> str:
    job_id = uuid.uuid4().hex
    IMPORT_JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "message": "Queued import",
        "result": None,
        "error": "",
    }

    thread = threading.Thread(
        target=_run_import_job,
        args=(job_id, lcsc_id, run_codex),
        daemon=True,
    )
    thread.start()
    return job_id


def _run_import_job(job_id: str, lcsc_id: str, run_codex: bool) -> None:
    def update(message: str) -> None:
        IMPORT_JOBS[job_id]["message"] = message

    IMPORT_JOBS[job_id].update(
        {
            "status": "running",
            "message": "Starting import",
        }
    )

    try:
        payload = import_lcsc_part(
            lcsc_id,
            run_codex=run_codex,
            status_callback=update,
        )
    except Exception as exc:
        IMPORT_JOBS[job_id].update(
            {
                "status": "error",
                "message": "Import failed",
                "error": str(exc),
            }
        )
        return

    IMPORT_JOBS[job_id].update(
        {
            "status": "complete",
            "message": "Import ready",
            "result": payload,
            "error": "",
        }
    )


def get_import_job(job_id: str) -> dict[str, Any]:
    return IMPORT_JOBS.get(job_id) or {
        "job_id": job_id,
        "status": "missing",
        "message": "Import job missing",
        "result": None,
        "error": "Unknown import job id.",
    }


def import_lcsc_part(
    lcsc_id: str,
    run_codex: bool = True,
    status_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    def status(message: str) -> None:
        if status_callback is not None:
            status_callback(message)

    status("Validating LCSC part number")
    lcsc_id = lcsc_id.strip().upper()
    if not re.fullmatch(r"C\d+", lcsc_id):
        raise ValueError("LCSC part number must look like C2040.")

    status("Fetching EasyEDA CAD data")
    api = EasyedaApi(use_cache=True)
    cad_data = api.get_cad_data_of_component(lcsc_id=lcsc_id)
    if not cad_data:
        raise RuntimeError(f"EasyEDA did not return CAD data for {lcsc_id}.")

    status("Converting EasyEDA symbol")
    ee_symbol = EasyedaSymbolImporter(easyeda_cp_cad_data=cad_data).get_symbol()
    status("Converting EasyEDA footprint")
    ee_footprint = EasyedaFootprintImporter(easyeda_cp_cad_data=cad_data).get_footprint()
    status("Preparing KiCad symbol")
    ki_symbol = ExporterSymbolKicad(
        symbol=ee_symbol,
        version=KICAD_VERSION,
    ).output
    ki_symbol.info.package = f"{DEFAULT_LIBRARY}:{ee_footprint.info.name}"

    status("Cleaning KLC symbol layout")
    cleaned_symbol, notes = clean_symbol(ki_symbol)
    unit_symbols: list[tuple[str, KiSymbol, list[str]]] = [
        ("Unit 1", cleaned_symbol, notes)
    ]
    for unit_index, sub_symbol in enumerate(ee_symbol.sub_symbols, start=2):
        sub_ki_symbol = ExporterSymbolKicad(
            symbol=sub_symbol,
            version=KICAD_VERSION,
        ).output
        sub_ki_symbol.info.package = f"{DEFAULT_LIBRARY}:{ee_footprint.info.name}"
        sub_cleaned_symbol, sub_notes = clean_symbol(sub_ki_symbol)
        unit_symbols.append((f"Unit {unit_index}", sub_cleaned_symbol, sub_notes))

    status("Fetching JLC part overview")
    overview = _part_overview(api, lcsc_id, cad_data)
    footprint = {
        "name": ee_footprint.info.name,
        "library": DEFAULT_LIBRARY,
        "field": f"{DEFAULT_LIBRARY}:{ee_footprint.info.name}",
        "type": ee_footprint.info.fp_type,
        "pad_count": len(ee_footprint.pads),
        "has_3d_model": ee_footprint.model_3d is not None,
        "model_name": ee_footprint.model_3d.name if ee_footprint.model_3d else "",
    }
    cleaned_symbol.info.package = footprint["field"]
    cleaned_symbol.info.manufacturer = (
        _clean_metadata_text(overview.get("manufacturer"))
        or cleaned_symbol.info.manufacturer
    )
    cleaned_symbol.info.mpn = _clean_metadata_text(overview.get("mpn")) or cleaned_symbol.info.mpn
    cleaned_symbol.info.datasheet = (
        _clean_metadata_text(overview.get("datasheet")) or cleaned_symbol.info.datasheet
    )
    cleaned_symbol.info.lcsc_id = lcsc_id
    cleaned_symbol.info.description = _description_from_overview(
        overview,
        footprint["name"],
        cleaned_symbol.info.description,
    )
    cleaned_symbol.info.keywords = (
        cleaned_symbol.info.keywords
        or _keywords_from_overview(overview, footprint["name"])
    )
    cleaned_symbol.info.fp_filters = _default_fp_filter(
        footprint["library"], footprint["name"]
    )
    for _, unit_symbol, _ in unit_symbols:
        unit_symbol.info = deepcopy(cleaned_symbol.info)

    payload = _symbol_to_payload(
        cleaned_symbol,
        lcsc_id=lcsc_id,
        overview=overview,
        footprint=footprint,
        notes=notes,
        units=unit_symbols if len(unit_symbols) > 1 else None,
    )

    if run_codex:
        status("Starting Codex datasheet pass")
        payload["codex_job_id"] = start_codex_pin_review(payload)

    status("Import ready")
    return payload


def _part_overview(
    api: EasyedaApi, lcsc_id: str, cad_data: dict[str, Any]
) -> dict[str, Any]:
    lcsc = cad_data.get("lcsc", {}) or {}
    head = cad_data.get("dataStr", {}).get("head", {}).get("c_para", {}) or {}
    overview = {
        "lcsc_id": lcsc_id,
        "name": head.get("name", ""),
        "mpn": head.get("Manufacturer Part", "") or head.get("BOM_Manufacturer Part", ""),
        "manufacturer": head.get("Manufacturer", "") or head.get("BOM_Manufacturer", ""),
        "package": head.get("package", ""),
        "datasheet": lcsc.get("url", "") or f"https://www.lcsc.com/datasheet/{lcsc_id}.pdf",
        "description": cad_data.get("description", ""),
        "attributes": [],
        "stock": "",
        "library_type": "",
    }

    try:
        search = api.search_jlcpcb_components(lcsc_id, page_size=1)
    except Exception:
        search = {"results": []}

    if search.get("results"):
        result = search["results"][0]
        overview.update(
            {
                "name": result.get("name") or overview["name"],
                "mpn": result.get("model") or overview["mpn"],
                "manufacturer": result.get("brand") or overview["manufacturer"],
                "package": result.get("package") or overview["package"],
                "datasheet": result.get("datasheet") or overview["datasheet"],
                "description": result.get("description") or overview["description"],
                "attributes": result.get("attributes") or [],
                "stock": result.get("stock", ""),
                "library_type": result.get("type", ""),
            }
        )

    return overview


def clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    existing_notes = list(payload.get("symbol", {}).get("notes") or [])
    symbol, notes = clean_symbol(_payload_to_symbol(payload))
    updated = deepcopy(payload)
    updated["symbol"]["pins"] = _symbol_to_payload(
        symbol,
        lcsc_id=str(payload.get("lcsc_id") or ""),
        overview=dict(payload.get("overview") or {}),
        footprint=dict(payload.get("footprint") or {}),
        notes=notes,
    )["symbol"]["pins"]
    _sync_active_unit_payload(updated)
    updated["symbol"]["notes"] = _merge_notes(notes, existing_notes)
    return updated


def start_codex_pin_review(payload: dict[str, Any]) -> str:
    job_id = uuid.uuid4().hex
    CODEX_JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "message": "Queued Codex datasheet pass",
        "result": None,
        "error": "",
        "elapsed_seconds": 0,
    }

    thread = threading.Thread(
        target=_run_codex_pin_review,
        args=(job_id, deepcopy(payload)),
        daemon=True,
    )
    thread.start()
    return job_id


def _codex_is_canceled(job_id: str) -> bool:
    return CODEX_JOBS.get(job_id, {}).get("status") == "canceled"


def cancel_all_codex_jobs() -> dict[str, Any]:
    canceled = 0
    for job_id, job in list(CODEX_JOBS.items()):
        if job.get("status") not in {"queued", "running"}:
            continue
        canceled += 1
        job.update(
            {
                "status": "canceled",
                "message": "Codex datasheet pass canceled",
                "error": "",
            }
        )
        process = CODEX_PROCESSES.pop(job_id, None)
        if process is not None and process.poll() is None:
            process.kill()
    return {"status": "ok", "canceled": canceled}


def _run_codex_pin_review(job_id: str, payload: dict[str, Any]) -> None:
    if _codex_is_canceled(job_id):
        return

    started_at = time.monotonic()
    CODEX_JOBS[job_id].update(
        {
            "status": "running",
            "message": "Codex is checking the datasheet",
            "started_at": started_at,
            "elapsed_seconds": 0,
        }
    )

    overview = payload.get("overview") or {}
    pins = [
        {
            "name": pin.get("name"),
            "number": pin.get("number"),
            "current_type": pin.get("type"),
        }
        for pin in payload.get("symbol", {}).get("pins", [])
    ]
    prompt = (
        "You are revising KiCad symbol pin electrical types from a component datasheet.\n"
        "Use the datasheet URL when available and determine KiCad ERC pin electrical types "
        "from the part documentation, not from package position or drawing placement.\n"
        "Classify every pin whose current_type is unspecified, passive, or otherwise likely wrong. "
        "Return unchanged pins too when the datasheet supports their current type. "
        "For a native KiCad pin stack number like [1,2], return that same stack number when all "
        "stacked pads share the same electrical function; otherwise return the individual pad "
        "numbers that need different types.\n"
        "Return only JSON with this shape:\n"
        "{\"pin_types\":[{\"number\":\"1 or [1,2]\",\"type\":\"input|output|bidirectional|"
        "tri_state|passive|free|unspecified|power_in|power_out|open_collector|"
        "open_emitter|no_connect\",\"reason\":\"short\"}],\"notes\":[\"short\"],"
        "\"status_line\":\"short status line for the UI\"}\n"
        "Set status_line to a short line describing what you concluded or where you are stuck.\n"
        "Do not change pin names or numbers. If the datasheet is unavailable, infer cautiously "
        "and say that in status_line.\n"
        f"Part overview:\n{json.dumps(overview, indent=2)}\n"
        f"Pins:\n{json.dumps(pins, indent=2)}\n"
    )

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            last_message = tmp_path / "codex_pin_types.json"
            schema_path = tmp_path / "pin_type_schema.json"
            schema_path.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "pin_types": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "number": {"type": "string"},
                                        "type": {
                                            "type": "string",
                                            "enum": PIN_TYPE_VALUES,
                                        },
                                        "reason": {"type": "string"},
                                    },
                                    "required": ["number", "type", "reason"],
                                },
                            },
                            "notes": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "status_line": {
                                "type": "string",
                            },
                        },
                        "required": ["pin_types", "notes", "status_line"],
                    }
                ),
                encoding="utf-8",
            )
            stdout_path = tmp_path / "codex_stdout.txt"
            stderr_path = tmp_path / "codex_stderr.txt"
            args = _codex_exec_args(schema_path, last_message)
            with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr_file:
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                )
                CODEX_PROCESSES[job_id] = process
                try:
                    try:
                        if process.stdin is not None:
                            process.stdin.write(prompt)
                            process.stdin.close()
                    except BrokenPipeError:
                        pass

                    last_status_at = 0.0
                    while True:
                        if _codex_is_canceled(job_id):
                            if process.poll() is None:
                                process.kill()
                                process.wait(timeout=5)
                            return

                        returncode = process.poll()
                        elapsed = time.monotonic() - started_at
                        if returncode is not None:
                            break

                        if elapsed > CODEX_TIMEOUT_SECONDS:
                            process.kill()
                            process.wait(timeout=5)
                            CODEX_JOBS[job_id].update(
                                {
                                    "status": "timeout",
                                    "message": "Codex datasheet pass timed out",
                                    "elapsed_seconds": int(elapsed),
                                    "error": (
                                        "codex exec timed out before returning pin type "
                                        f"suggestions after {_format_elapsed(elapsed)}."
                                    ),
                                }
                            )
                            return

                        if elapsed - last_status_at >= CODEX_STATUS_INTERVAL_SECONDS:
                            CODEX_JOBS[job_id].update(
                                {
                                    "message": (
                                        "Codex is checking the datasheet "
                                        f"({_format_elapsed(elapsed)})"
                                    ),
                                    "elapsed_seconds": int(elapsed),
                                }
                            )
                            last_status_at = elapsed

                        time.sleep(1)
                finally:
                    CODEX_PROCESSES.pop(job_id, None)

                if _codex_is_canceled(job_id):
                    return

            raw_result = (
                last_message.read_text(encoding="utf-8")
                if last_message.exists()
                else stdout_path.read_text(encoding="utf-8")
            )
            stdout_text = stdout_path.read_text(encoding="utf-8")
            stderr_text = stderr_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        CODEX_JOBS[job_id].update(
            {
                "status": "unavailable",
                "message": "Codex CLI unavailable",
                "error": "codex CLI was not found on PATH.",
            }
        )
        return
    except ValueError as exc:
        CODEX_JOBS[job_id].update(
            {
                "status": "error",
                "message": "Codex datasheet pass failed",
                "elapsed_seconds": int(time.monotonic() - started_at),
                "error": str(exc),
            }
        )
        return
    except subprocess.TimeoutExpired as exc:
        CODEX_JOBS[job_id].update(
            {
                "status": "timeout",
                "message": "Codex datasheet pass timed out",
                "elapsed_seconds": int(time.monotonic() - started_at),
                "error": str(exc),
            }
        )
        return

    if returncode != 0:
        CODEX_JOBS[job_id].update(
            {
                "status": "error",
                "message": "Codex datasheet pass failed",
                "elapsed_seconds": int(time.monotonic() - started_at),
                "error": stderr_text.strip() or stdout_text.strip(),
            }
        )
        return

    result = _extract_first_json(raw_result)
    if result is None:
        CODEX_JOBS[job_id].update(
            {
                "status": "error",
                "message": "Codex returned invalid JSON",
                "elapsed_seconds": int(time.monotonic() - started_at),
                "error": "codex exec did not return parseable JSON.",
                "raw": raw_result,
            }
        )
        return

    CODEX_JOBS[job_id].update(
        {
            "status": "complete",
            "result": result,
            "message": result.get("status_line") or "Codex datasheet pass complete",
            "elapsed_seconds": int(time.monotonic() - started_at),
            "error": "",
        }
    )


def _extract_first_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def get_codex_job(job_id: str) -> dict[str, Any]:
    job = CODEX_JOBS.get(job_id)
    if not job:
        return {
            "job_id": job_id,
            "status": "missing",
            "message": "Codex job missing",
            "result": None,
            "error": "Unknown job id.",
        }

    current = deepcopy(job)
    if current.get("status") == "running" and current.get("started_at") is not None:
        elapsed = int(time.monotonic() - float(current["started_at"]))
        current["elapsed_seconds"] = elapsed
        current["message"] = f"Codex is checking the datasheet ({_format_elapsed(elapsed)})"
    return current


def apply_codex_suggestions(
    payload: dict[str, Any], suggestions: dict[str, Any]
) -> dict[str, Any]:
    updated = deepcopy(payload)
    pins = updated.get("symbol", {}).get("pins", [])
    codex_notes: list[str] = []
    valid_entries: list[dict[str, Any]] = []

    status_line = str(suggestions.get("status_line") or "").strip()
    if status_line:
        codex_notes.append(f"Codex: {status_line}")

    for entry in suggestions.get("pin_types", []):
        entry_number = str(entry.get("number") or "").strip()
        pin_type = str(entry.get("type") or "")
        reason = str(entry.get("reason") or "").strip()
        if pin_type not in PIN_TYPE_VALUES:
            codex_notes.append(f"Codex ignored invalid pin type {pin_type} for {entry_number}.")
            continue
        valid_entries.append(
            {
                "number": entry_number,
                "numbers": set(_expand_stack_number(entry_number)),
                "type": pin_type,
                "reason": reason,
            }
        )

    pins, split_notes = _split_partial_stack_matches(pins, valid_entries)
    updated.setdefault("symbol", {})["pins"] = pins
    codex_notes.extend(split_notes)

    for entry in valid_entries:
        entry_number = str(entry["number"])
        pin_type = str(entry["type"])
        reason = str(entry["reason"])
        matches = _matching_payload_pins(pins, entry_number)
        if not matches:
            codex_notes.append(f"Codex could not match pin {entry_number}: {reason}")
            continue

        for pin in matches:
            old_type = str(pin.get("type") or "")
            pin["type"] = pin_type
            if old_type != pin_type:
                pin_name = str(pin.get("name") or "").strip()
                note = f"Codex set pin {pin.get('number')} {pin_name} to {pin_type}"
                if reason:
                    note += f": {reason}"
                codex_notes.append(note)

    for note in suggestions.get("notes", []):
        codex_notes.append(f"Codex note: {note}")

    updated.setdefault("symbol", {})["notes"] = _merge_notes(
        updated.get("symbol", {}).get("notes"),
        codex_notes,
    )
    return clean_payload(updated)


def _split_partial_stack_matches(
    pins: list[dict[str, Any]], entries: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    if not entries:
        return pins, []

    split_pins: list[dict[str, Any]] = []
    notes: list[str] = []
    for pin in pins:
        pin_number = str(pin.get("number") or "")
        pin_numbers = set(_expand_stack_number(pin_number))
        if len(pin_numbers) <= 1:
            split_pins.append(pin)
            continue

        matching_entries = [
            entry for entry in entries if pin_numbers.intersection(entry["numbers"])
        ]
        should_split = any(entry["numbers"] != pin_numbers for entry in matching_entries)
        if not should_split:
            split_pins.append(pin)
            continue

        for number in _expand_stack_number(pin_number):
            cloned = deepcopy(pin)
            cloned["number"] = number
            cloned["stacked_numbers"] = [number]
            split_pins.append(cloned)
        notes.append(
            f"Split native KiCad pin stack {pin_number} so Codex can assign individual electrical types."
        )

    return split_pins, notes


def _matching_payload_pins(
    pins: list[dict[str, Any]], entry_number: str
) -> list[dict[str, Any]]:
    if not entry_number:
        return []

    exact = [pin for pin in pins if str(pin.get("number") or "") == entry_number]
    if exact:
        return exact

    entry_numbers = set(_expand_stack_number(entry_number))
    if not entry_numbers:
        return []

    return [
        pin
        for pin in pins
        if entry_numbers.intersection(_expand_stack_number(str(pin.get("number") or "")))
    ]


def _write_footprint_assets_for_payload(
    root: Path,
    payload: dict[str, Any],
    api: EasyedaApi,
    cad_cache: dict[str, dict[str, Any]],
) -> None:
    lcsc_id = str(payload.get("lcsc_id") or "").strip().upper()
    if not re.fullmatch(r"C\d+", lcsc_id):
        raise ValueError("Bundle export requires a valid LCSC part number.")

    cad_data = cad_cache.get(lcsc_id)
    if cad_data is None:
        cad_data = api.get_cad_data_of_component(lcsc_id=lcsc_id)
        cad_cache[lcsc_id] = cad_data
    if not cad_data:
        raise RuntimeError(f"EasyEDA did not return CAD data for {lcsc_id}.")

    lib_name = _library_name(payload)
    ee_footprint = EasyedaFootprintImporter(easyeda_cp_cad_data=cad_data).get_footprint()
    footprint_for_export = deepcopy(ee_footprint)
    model_exporter: Exporter3dModelKicad | None = None
    model_3d_extension = "wrl"
    if ee_footprint.model_3d is not None:
        model_exporter = Exporter3dModelKicad(
            model_3d=Easyeda3dModelImporter(
                easyeda_cp_cad_data=cad_data,
                download_raw_3d_model=True,
                api=api,
            ).output,
        )
        if model_exporter.output and model_exporter.output.raw_wrl:
            model_3d_extension = "wrl"
        elif model_exporter.output_step:
            model_3d_extension = "step"
        else:
            footprint_for_export.model_3d = None

    pretty_dir = root / f"{lib_name}.pretty"
    footprint_path = pretty_dir / f"{ee_footprint.info.name}.kicad_mod"
    ExporterFootprintKicad(footprint=footprint_for_export).export(
        footprint_full_path=str(footprint_path),
        model_3d_path=f"${{KIPRJMOD}}/{lib_name}.3dshapes",
        model_3d_extension=model_3d_extension,
    )

    if model_exporter is not None and footprint_for_export.model_3d is not None:
        model_dir = root / f"{lib_name}.3dshapes"
        model_exporter.export(str(model_dir), overwrite=True)


def export_bundle_zip(payload: dict[str, Any]) -> bytes:
    symbol_payloads = _library_symbol_payloads(payload)
    if not symbol_payloads:
        raise ValueError("Bundle export requires at least one symbol.")

    library = _library_name(payload)
    normalized_payload = deepcopy(payload)
    normalized_payload["library"] = library
    symbol_text = export_symbol_library(normalized_payload)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sym_path = root / f"{library}.kicad_sym"
        sym_path.write_text(symbol_text, encoding="utf-8")

        api = EasyedaApi(use_cache=True)
        cad_cache: dict[str, dict[str, Any]] = {}
        for symbol_payload in symbol_payloads:
            symbol_payload = deepcopy(symbol_payload)
            symbol_payload["library"] = library
            _write_footprint_assets_for_payload(root, symbol_payload, api, cad_cache)

        stem = (
            library
            if _is_library_payload(payload)
            else sanitize_fields(str(symbol_payloads[0].get("lcsc_id") or library))
        )
        zip_path = root / f"{stem}_kicad_assets.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in root.rglob("*"):
                if file_path == zip_path or not file_path.is_file():
                    continue
                archive.write(file_path, file_path.relative_to(root).as_posix())

        return zip_path.read_bytes()


def filename_for_payload(payload: dict[str, Any], suffix: str) -> str:
    if _is_library_payload(payload):
        return f"{sanitize_fields(_library_name(payload))}{suffix}"
    info = payload.get("symbol", {}).get("info", {})
    stem = sanitize_fields(str(info.get("name") or payload.get("lcsc_id") or "symbol"))
    return f"{stem}{suffix}"
