"""Lab L4: normalización de números de WhatsApp argentinos."""

import pytest

from app.features.whatsapp.phone import last_digits
from app.features.whatsapp.phone import normalize_phone


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("5491155551234", "5491155551234"),  # wa_id con 9, tal cual
        ("+5491155551234", "5491155551234"),  # con +
        ("+54 9 11 5555-1234", "5491155551234"),  # como lo carga el comprador
        ("541155551234", "5491155551234"),  # wa_id sin el 9 de celular
        ("+54 11 5555 1234", "5491155551234"),  # sin 9, con espacios
        ("005491155551234", "5491155551234"),  # prefijo internacional 00
        ("54 (11) 5555-1234", "5491155551234"),
        ("5511999999999", "5511999999999"),  # Brasil: no se toca
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_normalization_is_idempotent():
    once = normalize_phone("+54 11 5555-1234")

    assert normalize_phone(once) == once == "5491155551234"


def test_last_digits_for_logs():
    assert last_digits("+54 9 11 5555-1234") == "1234"
    assert last_digits("12") == "12"
    assert last_digits(None) == ""
