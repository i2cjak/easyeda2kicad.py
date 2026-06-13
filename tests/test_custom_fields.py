from __future__ import annotations

import pytest

from easyeda2kicad.__main__ import parse_custom_fields
from easyeda2kicad.kicad.parameters_kicad_symbol import KiSymbolInfo


def test_parse_custom_fields_last_wins() -> None:
    assert parse_custom_fields(
        ["Manufacturer:Texas Instruments", "Manufacturer:TI", "LCSC ID:C2040"]
    ) == {
        "Manufacturer": "TI",
        "LCSC ID": "C2040",
    }


@pytest.mark.parametrize(
    "value",
    [
        "Manufacturer",
        ":Texas Instruments",
        "   :Texas Instruments",
    ],
)
def test_parse_custom_fields_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_custom_fields([value])


def test_symbol_export_includes_custom_fields() -> None:
    symbol = KiSymbolInfo(
        name="TestPart",
        prefix="U",
        package="Lib:Footprint",
        manufacturer="",
        datasheet="",
        lcsc_id="C2040",
        custom_fields={
            "Manufacturer": "Texas Instruments",
            "Package": "LQFN-56",
        },
    )

    exported = "\n".join(symbol.export())

    assert '"Manufacturer"' in exported
    assert '"Texas Instruments"' in exported
    assert "(id 10)" in exported
    assert '"Package"' in exported
    assert '"LQFN-56"' in exported
    assert "(id 11)" in exported


def test_symbol_export_includes_fp_filters_and_escapes_fields() -> None:
    symbol = KiSymbolInfo(
        name="TestPart",
        prefix="U",
        package="Lib:Footprint",
        manufacturer='ACME "Quoted"',
        datasheet="https://example.com/ds.pdf",
        lcsc_id="C2040",
        description='Buck "converter"',
        fp_filters="Lib:QFN*7x7mm*",
    )

    exported = "\n".join(symbol.export())

    assert '"ki_fp_filters"' in exported
    assert '"Lib:QFN*7x7mm*"' in exported
    assert "(id 7)" in exported
    assert 'ACME \\"Quoted\\"' in exported
    assert 'Buck \\"converter\\"' in exported
