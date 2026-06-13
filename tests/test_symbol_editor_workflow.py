from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import time
import zipfile

import pytest

from symbol_editor_app import create_app
from easyeda2kicad.easyeda.parameters_easyeda import Ee3dModel, Ee3dModelBase
from easyeda2kicad.kicad.export_kicad_3d_model import Exporter3dModelKicad
from easyeda2kicad.kicad.parameters_kicad_symbol import (
    KICAD_SYM_VERSION_20251024,
    KiPinStyle,
    KiPinType,
    KiSymbol,
    KiSymbolInfo,
    KiSymbolPin,
)
from symbol_editor_app.workflow import (
    CODEX_JOBS,
    CODEX_PROCESSES,
    CODEX_PROFILE_ENV,
    DEFAULT_CODEX_PROFILE,
    _asset_manifest_for_payload,
    _codex_exec_args,
    _default_fp_filter,
    _run_codex_pin_review,
    apply_codex_suggestions,
    cancel_all_codex_jobs,
    clean_symbol,
    export_bundle_zip,
    export_symbol_library,
    get_codex_job,
    validate_symbol_payload,
)


def _pin(
    name: str,
    number: str,
    pin_type: KiPinType,
) -> KiSymbolPin:
    return KiSymbolPin(
        name=name,
        number=number,
        style=KiPinStyle.line,
        length=2.54,
        type=pin_type,
        orientation=180,
        pos_x=0,
        pos_y=0,
    )


def _actual_orientation(pin: KiSymbolPin) -> int:
    return int((180 + pin.orientation) % 360)


def _payload() -> dict:
    return {
        "lcsc_id": "C1",
        "library": "easyeda2kicad",
        "symbol": {
            "info": {
                "name": "TEST_PART",
                "prefix": "U",
                "package": "easyeda2kicad:TEST_FOOTPRINT",
                "manufacturer": "",
                "datasheet": "",
                "lcsc_id": "C1",
                "mpn": "",
                "keywords": "",
                "description": "",
            },
            "custom_fields": {},
            "pins": [
                {
                    "name": "GND",
                    "number": "1",
                    "type": "power_in",
                    "style": "line",
                    "length": 2.54,
                    "x": 0,
                    "y": -6.35,
                    "orientation": 90,
                }
            ],
        },
    }


def test_cleanup_uses_native_bracket_pin_stacks() -> None:
    symbol = KiSymbol(
        info=KiSymbolInfo(
            name="TEST_PART",
            prefix="U",
            package="",
            manufacturer="",
            datasheet="",
            lcsc_id="C1",
        ),
        pins=[
            _pin("GND", "2", KiPinType.power_in),
            _pin("GND", "1", KiPinType.power_in),
            _pin("VCC", "3", KiPinType.power_in),
        ],
    )

    cleaned, notes = clean_symbol(symbol)
    gnd_pins = [pin for pin in cleaned.pins if pin.name == "GND"]

    assert len(gnd_pins) == 1
    assert gnd_pins[0].number == "[1,2]"
    assert gnd_pins[0].hidden is False
    assert any("native KiCad number [1,2]" in note for note in notes)

    exported = cleaned.export(version=KICAD_SYM_VERSION_20251024)
    assert '(number "[1,2]"' in exported
    assert exported.count("(pin power_in line") == 2


def test_cleanup_places_pins_on_klc_sides() -> None:
    symbol = KiSymbol(
        info=KiSymbolInfo(
            name="TEST_PART",
            prefix="U",
            package="",
            manufacturer="",
            datasheet="",
            lcsc_id="C1",
        ),
        pins=[
            _pin("VIN", "1", KiPinType.power_in),
            _pin("GND", "2", KiPinType.power_in),
            _pin("EN", "3", KiPinType._input),
            _pin("OUT", "4", KiPinType.output),
        ],
    )

    cleaned, _ = clean_symbol(symbol)
    by_name = {pin.name: pin for pin in cleaned.pins}

    assert by_name["VIN"].pos_y > 0
    assert _actual_orientation(by_name["VIN"]) == 270
    assert by_name["GND"].pos_y < 0
    assert _actual_orientation(by_name["GND"]) == 90
    assert by_name["EN"].pos_x < 0
    assert _actual_orientation(by_name["EN"]) == 0
    assert by_name["OUT"].pos_x > 0
    assert _actual_orientation(by_name["OUT"]) == 180


def test_cleanup_keeps_even_pin_groups_on_100mil_grid() -> None:
    symbol = KiSymbol(
        info=KiSymbolInfo(
            name="TEST_PART",
            prefix="U",
            package="",
            manufacturer="",
            datasheet="",
            lcsc_id="C1",
        ),
        pins=[
            _pin("EN", "1", KiPinType._input),
            _pin("MODE", "2", KiPinType._input),
            _pin("GND", "3", KiPinType.power_in),
            _pin("VREG_PGND", "4", KiPinType.power_in),
        ],
    )

    cleaned, _ = clean_symbol(symbol)

    for pin in cleaned.pins:
        assert round(pin.pos_x / 2.54) == pytest.approx(pin.pos_x / 2.54)
        assert round(pin.pos_y / 2.54) == pytest.approx(pin.pos_y / 2.54)


def test_cleanup_promotes_obvious_supply_names_to_power_pins() -> None:
    symbol = KiSymbol(
        info=KiSymbolInfo(
            name="TEST_PART",
            prefix="U",
            package="",
            manufacturer="",
            datasheet="",
            lcsc_id="C1",
        ),
        pins=[
            _pin("ADC_AVDD", "1", KiPinType.bidirectional),
            _pin("IOVDD", "2", KiPinType.bidirectional),
            _pin("USB_GND", "3", KiPinType.bidirectional),
        ],
    )

    cleaned, _ = clean_symbol(symbol)
    by_name = {pin.name: pin for pin in cleaned.pins}

    assert by_name["ADC_AVDD"].type == KiPinType.power_in
    assert by_name["ADC_AVDD"].pos_y > 0
    assert by_name["IOVDD"].type == KiPinType.power_in
    assert by_name["IOVDD"].pos_y > 0
    assert by_name["USB_GND"].type == KiPinType.power_in
    assert by_name["USB_GND"].pos_y < 0


def test_symbol_library_export_contains_kicad10_header() -> None:
    payload = _payload()
    payload["symbol"]["info"]["package"] = ""
    payload["symbol"]["pins"][0]["number"] = "[1,2]"

    exported = export_symbol_library(payload)

    assert "(kicad_symbol_lib" in exported
    assert "(version 20251024)" in exported
    assert '(number "[1,2]"' in exported


def test_symbol_library_export_uses_klc_body_stroke_width() -> None:
    exported = export_symbol_library(_payload())

    assert "(stroke (width 0.254) (type default))" in exported


def test_symbol_library_export_includes_editable_fp_filters() -> None:
    payload = {
        "lcsc_id": "C1",
        "symbol": {
            "info": {
                "name": "TEST_PART",
                "prefix": "U",
                "package": "easyeda2kicad:QFN-60_L7.0-W7.0-P0.40-TL-EP3.4",
                "manufacturer": "",
                "datasheet": "https://example.com/ds.pdf",
                "lcsc_id": "C1",
                "mpn": "",
                "keywords": "",
                "description": "Imported test part",
                "fp_filters": _default_fp_filter(
                    "easyeda2kicad", "QFN-60_L7.0-W7.0-P0.40-TL-EP3.4"
                ),
            },
            "custom_fields": {},
            "pins": [
                {
                    "name": "GND",
                    "number": "1",
                    "type": "power_in",
                    "style": "line",
                    "length": 2.54,
                    "x": 0,
                    "y": -6.35,
                    "orientation": 90,
                }
            ],
        },
    }

    exported = export_symbol_library(payload)

    assert '"ki_fp_filters"' in exported
    assert '"easyeda2kicad:QFN*60*L7.0*W7.0*P0.40*TL*EP3.4*"' in exported


def test_symbol_library_export_combines_multiple_payloads() -> None:
    payload_a = _payload()
    payload_b = _payload()
    payload_b["lcsc_id"] = "C2"
    payload_b["symbol"]["info"]["name"] = "TEST_PART_B"
    payload_b["symbol"]["info"]["lcsc_id"] = "C2"

    exported = export_symbol_library(
        {
            "library": "batch_lib",
            "symbols": [
                {"id": "a", "payload": payload_a},
                {"id": "b", "payload": payload_b},
            ],
        }
    )

    assert exported.count("(kicad_symbol_lib") == 1
    assert '(symbol "TEST_PART"' in exported
    assert '(symbol "TEST_PART_B"' in exported
    assert "(version 20251024)" in exported


def test_multi_unit_symbol_export_uses_numbered_unit_blocks() -> None:
    payload = _payload()
    payload["symbol"]["units"] = [
        {
            "name": "A",
            "pins": [
                {
                    "name": "INA",
                    "number": "1",
                    "type": "input",
                    "style": "line",
                    "length": 2.54,
                    "x": -7.62,
                    "y": 0,
                    "orientation": 0,
                }
            ],
        },
        {
            "name": "B",
            "pins": [
                {
                    "name": "OUTB",
                    "number": "2",
                    "type": "output",
                    "style": "line",
                    "length": 2.54,
                    "x": 7.62,
                    "y": 0,
                    "orientation": 180,
                }
            ],
        },
    ]
    payload["symbol"]["active_unit"] = 0
    payload["symbol"]["pins"] = payload["symbol"]["units"][0]["pins"]

    exported = export_symbol_library(payload)

    assert '(symbol "TEST_PART_1_1"' in exported
    assert '(symbol "TEST_PART_2_1"' in exported
    assert '(symbol "TEST_PART_0_1"' not in exported
    assert '(number "1"' in exported
    assert '(number "2"' in exported


def test_asset_manifest_tracks_symbol_footprint_and_pending_model() -> None:
    payload = _payload()
    payload["footprint"] = {
        "name": "TEST_FOOTPRINT",
        "field": "easyeda2kicad:TEST_FOOTPRINT",
        "has_3d_model": True,
        "model_name": "TEST_MODEL",
    }

    manifest = _asset_manifest_for_payload(payload)

    assert manifest["symbol_file"] == "easyeda2kicad.kicad_sym"
    assert manifest["footprint_file"] == "easyeda2kicad.pretty/TEST_FOOTPRINT.kicad_mod"
    assert manifest["model_dir"] == "easyeda2kicad.3dshapes"
    assert manifest["model_name"] == "TEST_MODEL"
    assert manifest["model_status"] == "pending"
    assert manifest["model_files"] == []


def test_3d_model_export_writes_step_when_wrl_is_unavailable(tmp_path) -> None:
    model = Ee3dModel(
        name="TEST_MODEL",
        uuid="uuid",
        translation=Ee3dModelBase(),
        rotation=Ee3dModelBase(),
        raw_obj=None,
        step=b"ISO-10303-21;",
    )

    assert Exporter3dModelKicad(model).export(str(tmp_path)) is True
    assert (tmp_path / "TEST_MODEL.step").read_bytes() == b"ISO-10303-21;"
    assert not (tmp_path / "TEST_MODEL.wrl").exists()


def test_bundle_export_skips_3d_lookup_when_footprint_has_no_model(monkeypatch) -> None:
    class FakeApi:
        def __init__(self, use_cache: bool) -> None:
            self.use_cache = use_cache

        def get_cad_data_of_component(self, lcsc_id: str) -> dict:
            return {"ok": True, "lcsc_id": lcsc_id}

    class FakeFootprintImporter:
        def __init__(self, easyeda_cp_cad_data: dict) -> None:
            self.easyeda_cp_cad_data = easyeda_cp_cad_data

        def get_footprint(self) -> SimpleNamespace:
            return SimpleNamespace(
                info=SimpleNamespace(name="TEST_FOOTPRINT", fp_type="smd"),
                pads=[],
                model_3d=None,
            )

    class FakeFootprintExporter:
        def __init__(self, footprint: SimpleNamespace) -> None:
            self.footprint = footprint

        def export(
            self,
            footprint_full_path: str,
            model_3d_path: str,
            model_3d_extension: str = "wrl",
        ) -> None:
            assert self.footprint.model_3d is None
            Path(footprint_full_path).parent.mkdir(parents=True, exist_ok=True)
            with open(footprint_full_path, "w", encoding="utf-8") as out:
                out.write("(module TEST_FOOTPRINT)")

    def fail_3d_importer(*args, **kwargs):
        raise AssertionError("3D importer should not run without a footprint model")

    monkeypatch.setattr("symbol_editor_app.workflow.EasyedaApi", FakeApi)
    monkeypatch.setattr(
        "symbol_editor_app.workflow.EasyedaFootprintImporter", FakeFootprintImporter
    )
    monkeypatch.setattr(
        "symbol_editor_app.workflow.ExporterFootprintKicad", FakeFootprintExporter
    )
    monkeypatch.setattr("symbol_editor_app.workflow.Easyeda3dModelImporter", fail_3d_importer)

    with zipfile.ZipFile(BytesIO(export_bundle_zip(_payload()))) as archive:
        names = sorted(archive.namelist())

    assert "easyeda2kicad.kicad_sym" in names
    assert "easyeda2kicad.pretty/TEST_FOOTPRINT.kicad_mod" in names
    assert not any(".3dshapes/" in name for name in names)


def test_bundle_export_uses_step_reference_for_step_only_model(monkeypatch) -> None:
    captured = {}

    class FakeApi:
        def __init__(self, use_cache: bool) -> None:
            self.use_cache = use_cache

        def get_cad_data_of_component(self, lcsc_id: str) -> dict:
            return {"ok": True, "lcsc_id": lcsc_id}

    class FakeFootprintImporter:
        def __init__(self, easyeda_cp_cad_data: dict) -> None:
            self.easyeda_cp_cad_data = easyeda_cp_cad_data

        def get_footprint(self) -> SimpleNamespace:
            return SimpleNamespace(
                info=SimpleNamespace(name="TEST_FOOTPRINT", fp_type="smd"),
                pads=[],
                model_3d=Ee3dModel(
                    name="TEST_MODEL",
                    uuid="uuid",
                    translation=Ee3dModelBase(),
                    rotation=Ee3dModelBase(),
                ),
            )

    class FakeModelImporter:
        def __init__(self, *args, **kwargs) -> None:
            self.output = Ee3dModel(
                name="TEST_MODEL",
                uuid="uuid",
                translation=Ee3dModelBase(),
                rotation=Ee3dModelBase(),
                raw_obj=None,
                step=b"ISO-10303-21;",
            )

    class FakeFootprintExporter:
        def __init__(self, footprint: SimpleNamespace) -> None:
            self.footprint = footprint

        def export(
            self,
            footprint_full_path: str,
            model_3d_path: str,
            model_3d_extension: str = "wrl",
        ) -> None:
            captured["model_3d_extension"] = model_3d_extension
            Path(footprint_full_path).parent.mkdir(parents=True, exist_ok=True)
            with open(footprint_full_path, "w", encoding="utf-8") as out:
                out.write(f"(model TEST_MODEL.{model_3d_extension})")

    monkeypatch.setattr("symbol_editor_app.workflow.EasyedaApi", FakeApi)
    monkeypatch.setattr(
        "symbol_editor_app.workflow.EasyedaFootprintImporter", FakeFootprintImporter
    )
    monkeypatch.setattr(
        "symbol_editor_app.workflow.Easyeda3dModelImporter", FakeModelImporter
    )
    monkeypatch.setattr(
        "symbol_editor_app.workflow.ExporterFootprintKicad", FakeFootprintExporter
    )

    with zipfile.ZipFile(BytesIO(export_bundle_zip(_payload()))) as archive:
        names = sorted(archive.namelist())

    assert captured["model_3d_extension"] == "step"
    assert "easyeda2kicad.3dshapes/TEST_MODEL.step" in names
    assert "easyeda2kicad.3dshapes/TEST_MODEL.wrl" not in names


def test_validate_symbol_payload_runs_kicad_cli_and_reports_checks(monkeypatch) -> None:
    calls = []
    payload = _payload()
    payload["symbol"]["pins"][0]["y"] = -7.62
    bundle_io = BytesIO()
    with zipfile.ZipFile(bundle_io, "w") as archive:
        archive.writestr("easyeda2kicad.kicad_sym", "(kicad_symbol_lib)")
        archive.writestr(
            "easyeda2kicad.pretty/TEST_FOOTPRINT.kicad_mod",
            '(footprint "TEST_FOOTPRINT" (model "${KIPRJMOD}/easyeda2kicad.3dshapes/TEST.step"))',
        )
        archive.writestr("easyeda2kicad.3dshapes/TEST.step", "ISO-10303-21;")

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:4] == ["kicad-cli", "sym", "export", "svg"]:
            output_dir = Path(args[args.index("--output") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "TEST_PART.svg").write_text("<svg/>", encoding="utf-8")
        if args[:4] == ["kicad-cli", "fp", "export", "svg"]:
            output_dir = Path(args[args.index("--output") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "TEST_FOOTPRINT.svg").write_text("<svg footprint=\"1\"/>", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("symbol_editor_app.workflow.subprocess.run", fake_run)
    monkeypatch.setattr(
        "symbol_editor_app.workflow.export_bundle_zip",
        lambda payload: bundle_io.getvalue(),
    )

    result = validate_symbol_payload(payload)

    assert result["status"] == "ok"
    assert ["kicad-cli", "sym", "upgrade", "--force"] == calls[0][:4]
    assert calls[1][:4] == ["kicad-cli", "sym", "export", "svg"]
    assert calls[2][:4] == ["kicad-cli", "fp", "upgrade", "--force"]
    assert calls[3][:4] == ["kicad-cli", "fp", "export", "svg"]
    assert result["svg"] == "<svg/>"
    assert result["footprint_svg"] == '<svg footprint="1"/>'
    assert result["assets"]["symbol_file"] == "easyeda2kicad.kicad_sym"
    assert result["assets"]["footprint_file"] == "easyeda2kicad.pretty/TEST_FOOTPRINT.kicad_mod"
    assert result["assets"]["model_files"] == ["easyeda2kicad.3dshapes/TEST.step"]
    assert result["assets"]["model_refs"] == ["${KIPRJMOD}/easyeda2kicad.3dshapes/TEST.step"]
    assert result["assets"]["model_status"] == "included"
    assert any(check["message"] == "KiCad accepted the symbol library file." for check in result["checks"])
    assert any("KiCad rendered SVG preview" in check["message"] for check in result["checks"])
    assert any("3D model references resolve" in check["message"] for check in result["checks"])


def test_validate_route_returns_validation_result(monkeypatch) -> None:
    pytest.importorskip("flask")
    monkeypatch.setattr(
        "symbol_editor_app.routes.validate_symbol_payload",
        lambda payload: {"status": "ok", "message": "Validation passed", "checks": []},
    )
    app = create_app()

    with app.test_client() as client:
        response = client.post("/api/validate", json=_payload())

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_export_routes_reject_validation_errors(monkeypatch) -> None:
    pytest.importorskip("flask")

    def fake_validate(payload):
        return {
            "status": "error",
            "message": "Validation failed",
            "checks": [
                {
                    "level": "error",
                    "message": "Symbol footprint field points to missing easyeda2kicad.pretty/WRONG.kicad_mod.",
                }
            ],
        }

    monkeypatch.setattr("symbol_editor_app.routes.validate_symbol_payload", fake_validate)
    monkeypatch.setattr(
        "symbol_editor_app.routes.export_bundle_zip",
        lambda payload: (_ for _ in ()).throw(AssertionError("export should be gated")),
    )
    app = create_app()

    with app.test_client() as client:
        response = client.post("/api/export/bundle", json=_payload())

    assert response.status_code == 400
    assert "missing easyeda2kicad.pretty/WRONG.kicad_mod" in response.get_json()["error"]


def test_export_symbol_route_allows_validation_warnings(monkeypatch) -> None:
    pytest.importorskip("flask")
    monkeypatch.setattr(
        "symbol_editor_app.routes.validate_symbol_payload",
        lambda payload: {"status": "warn", "message": "Validation passed with warnings", "checks": []},
    )
    monkeypatch.setattr(
        "symbol_editor_app.routes.export_symbol_library",
        lambda payload: "(kicad_symbol_lib)\n",
    )
    app = create_app()

    with app.test_client() as client:
        response = client.post("/api/export/symbol", json=_payload())

    assert response.status_code == 200
    assert response.data == b"(kicad_symbol_lib)\n"


def test_import_route_preserves_checked_codex_pass(monkeypatch) -> None:
    pytest.importorskip("flask")
    captured = {}

    def fake_start_import_job(lcsc_id: str, run_codex: bool = True) -> str:
        captured["lcsc_id"] = lcsc_id
        captured["run_codex"] = run_codex
        return "import-job"

    monkeypatch.setattr("symbol_editor_app.routes.start_import_job", fake_start_import_job)
    monkeypatch.setattr(
        "symbol_editor_app.routes.get_import_job",
        lambda job_id: {
            "job_id": job_id,
            "status": "queued",
            "message": "Queued import",
            "result": None,
            "error": "",
        },
    )
    app = create_app()

    with app.test_client() as client:
        response = client.post(
            "/api/import",
            json={"lcsc_id": "C42411118", "run_codex": True},
        )

    assert response.status_code == 202
    assert captured == {"lcsc_id": "C42411118", "run_codex": True}


def test_codex_exec_args_use_default_plain_profile(tmp_path) -> None:
    args = _codex_exec_args(tmp_path / "schema.json", tmp_path / "last.json")

    assert args[:6] == ["codex", "--search", "-a", "never", "-p", DEFAULT_CODEX_PROFILE]
    assert args[6] == "exec"
    assert args[-1] == "-"


def test_codex_exec_args_allow_profile_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(CODEX_PROFILE_ENV, "datasheet")

    args = _codex_exec_args(tmp_path / "schema.json", tmp_path / "last.json")

    assert args[:6] == ["codex", "--search", "-a", "never", "-p", "datasheet"]
    assert args[6] == "exec"
    assert args[-1] == "-"


def test_codex_exec_args_allow_profile_disable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(CODEX_PROFILE_ENV, "none")

    args = _codex_exec_args(tmp_path / "schema.json", tmp_path / "last.json")

    assert args[:5] == ["codex", "--search", "-a", "never", "exec"]
    assert "-p" not in args
    assert args[-1] == "-"


def test_codex_exec_args_reject_prompt_shaped_profile(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(CODEX_PROFILE_ENV, "You are revising KiCad pins")

    with pytest.raises(ValueError, match="plain Codex profile"):
        _codex_exec_args(tmp_path / "schema.json", tmp_path / "last.json")


def test_codex_pin_review_uses_exec_stdin_not_profile(monkeypatch) -> None:
    captured = {}
    monkeypatch.setenv(CODEX_PROFILE_ENV, "none")

    class FakeStdin:
        def write(self, text):
            captured["input"] = text

        def close(self):
            captured["stdin_closed"] = True

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            self.stdin = FakeStdin()
            output_path = args[args.index("--output-last-message") + 1]
            with open(output_path, "w", encoding="utf-8") as out:
                out.write('{"pin_types":[],"notes":[],"status_line":"Checked pins."}')

        def poll(self):
            return 0

        def kill(self):
            captured["killed"] = True

        def wait(self, timeout=None):
            return 0

    def fake_sleep(seconds):
        captured["sleep"] = seconds

    monkeypatch.setattr("symbol_editor_app.workflow.subprocess.Popen", FakePopen)
    monkeypatch.setattr("symbol_editor_app.workflow.time.sleep", fake_sleep)

    def forbidden_run(*args, **kwargs):
        raise AssertionError("Codex runner should use Popen for live status updates")

    monkeypatch.setattr("symbol_editor_app.workflow.subprocess.run", forbidden_run)

    job_id = "test-job"
    CODEX_JOBS[job_id] = {"status": "queued", "result": None, "error": ""}
    _run_codex_pin_review(
        job_id,
        {
            "overview": {"datasheet": "https://example.com/part.pdf"},
            "symbol": {
                "pins": [
                    {"name": "RUN", "number": "1", "type": "unspecified"},
                ]
            },
        },
    )

    assert captured["args"][:5] == [
        "codex",
        "--search",
        "-a",
        "never",
        "exec",
    ]
    assert "-p" not in captured["args"]
    assert "--ask-for-approval" not in captured["args"]
    assert captured["args"][-1] == "-"
    assert "You are revising KiCad symbol pin electrical types" in captured["input"]
    assert "status_line" in captured["input"]
    assert captured["stdin_closed"] is True
    assert CODEX_JOBS[job_id]["status"] == "complete"
    assert CODEX_JOBS[job_id]["message"] == "Checked pins."


def test_codex_running_job_reports_elapsed_status() -> None:
    CODEX_JOBS["elapsed-job"] = {
        "job_id": "elapsed-job",
        "status": "running",
        "message": "Codex is checking the datasheet",
        "result": None,
        "error": "",
        "started_at": 10.0,
    }

    original_monotonic = time.monotonic
    try:
        time.monotonic = lambda: 75.0
        job = get_codex_job("elapsed-job")
    finally:
        time.monotonic = original_monotonic

    assert job["elapsed_seconds"] == 65
    assert job["message"] == "Codex is checking the datasheet (1m 05s)"


def test_cancel_all_codex_jobs_marks_running_jobs_and_kills_process() -> None:
    CODEX_JOBS.clear()
    CODEX_PROCESSES.clear()

    class FakeProcess:
        def __init__(self) -> None:
            self.killed = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    process = FakeProcess()
    CODEX_JOBS["cancel-job"] = {
        "job_id": "cancel-job",
        "status": "running",
        "message": "Codex is checking the datasheet",
        "result": None,
        "error": "",
    }
    CODEX_PROCESSES["cancel-job"] = process

    result = cancel_all_codex_jobs()

    assert result == {"status": "ok", "canceled": 1}
    assert CODEX_JOBS["cancel-job"]["status"] == "canceled"
    assert process.killed is True
    assert "cancel-job" not in CODEX_PROCESSES


def test_codex_start_route_reviews_current_payload(monkeypatch) -> None:
    pytest.importorskip("flask")
    monkeypatch.setattr(
        "symbol_editor_app.routes.start_codex_pin_review",
        lambda payload: "manual-job",
    )
    CODEX_JOBS["manual-job"] = {
        "status": "queued",
        "message": "Queued Codex datasheet pass",
        "result": None,
        "error": "",
    }
    app = create_app()

    with app.test_client() as client:
        response = client.post(
            "/api/codex/start",
            json={
                "payload": {
                    "symbol": {"pins": []},
                    "overview": {"datasheet": "https://example.com/ds.pdf"},
                }
            },
        )

    assert response.status_code == 202
    assert response.get_json()["job_id"] == "manual-job"
    assert response.get_json()["status"] == "queued"


def test_codex_apply_matches_numbers_inside_native_stacks_and_keeps_notes() -> None:
    payload = {
        "lcsc_id": "C1",
        "symbol": {
            "info": {
                "name": "TEST_PART",
                "prefix": "U",
                "package": "",
                "manufacturer": "",
                "datasheet": "",
                "lcsc_id": "C1",
                "mpn": "",
                "keywords": "",
                "description": "",
            },
            "custom_fields": {},
            "pins": [
                {
                    "name": "DATA",
                    "number": "[1,2]",
                    "type": "unspecified",
                    "style": "line",
                    "length": 2.54,
                    "x": -7.62,
                    "y": 0,
                    "orientation": 0,
                }
            ],
            "notes": ["Existing note"],
        },
    }

    updated = apply_codex_suggestions(
        payload,
        {
            "pin_types": [
                {
                    "number": "[1,2]",
                    "type": "bidirectional",
                    "reason": "DATA stack pads are bidirectional.",
                }
            ],
            "notes": ["Reviewed from datasheet."],
            "status_line": "Checked one stack.",
        },
    )

    pin = updated["symbol"]["pins"][0]
    assert pin["number"] == "[1,2]"
    assert pin["type"] == "bidirectional"
    assert "Existing note" in updated["symbol"]["notes"]
    assert any("Codex set pin [1,2] DATA to bidirectional" in note for note in updated["symbol"]["notes"])
    assert "Codex note: Reviewed from datasheet." in updated["symbol"]["notes"]


def test_codex_apply_splits_native_stack_for_individual_pin_types() -> None:
    payload = {
        "lcsc_id": "C1",
        "symbol": {
            "info": {
                "name": "TEST_PART",
                "prefix": "U",
                "package": "",
                "manufacturer": "",
                "datasheet": "",
                "lcsc_id": "C1",
                "mpn": "",
                "keywords": "",
                "description": "",
            },
            "custom_fields": {},
            "pins": [
                {
                    "name": "DATA",
                    "number": "[1,2]",
                    "type": "unspecified",
                    "style": "line",
                    "length": 2.54,
                    "x": -7.62,
                    "y": 0,
                    "orientation": 0,
                }
            ],
            "notes": [],
        },
    }

    updated = apply_codex_suggestions(
        payload,
        {
            "pin_types": [
                {
                    "number": "2",
                    "type": "bidirectional",
                    "reason": "Only pad 2 is bidirectional.",
                }
            ],
            "notes": [],
            "status_line": "Checked split stack.",
        },
    )

    by_number = {pin["number"]: pin for pin in updated["symbol"]["pins"]}
    assert by_number["1"]["type"] == "unspecified"
    assert by_number["2"]["type"] == "bidirectional"
    assert any("Split native KiCad pin stack [1,2]" in note for note in updated["symbol"]["notes"])
