"""Genera los Excel de ejemplo del intake de materiales (lab L2).

Correr desde ``backend/``::

    uv run python -m examples.make_examples

Produce dos archivos en esta misma carpeta:

* ``materiales_mamposteria.xlsx``  cinco ítems válidos: el caso feliz.
* ``materiales_con_errores.xlsx``  tres errores a la vez, para los tests:
  falta el encabezado ``accepted_alternatives``, una cantidad en texto ("veinte")
  y un ítem repetido.

Los archivos están commiteados; este script existe para regenerarlos si cambia el
formato, no para correrlo en cada arranque.
"""

from pathlib import Path

from openpyxl import Workbook

HERE = Path(__file__).resolve().parent

HEADERS = ["item", "quantity", "unit", "specification", "accepted_alternatives"]

MAMPOSTERIA = [
    [
        "Ladrillo hueco portante 18x19x33",
        1200,
        "un",
        "Ladrillo cerámico hueco portante 18x19x33 cm, clase resistente según IRAM 12566",
        "Ladrillo hueco portante 18x18x33",
    ],
    [
        "Cemento CPN40",
        60,
        "bolsa 50 kg",
        "Cemento portland normal CPN40, bolsa de 50 kg, IRAM 50000",
        "CPN50",
    ],
    [
        "Cal hidráulica",
        40,
        "bolsa 25 kg",
        "Cal hidráulica hidratada en polvo, bolsa de 25 kg",
        "Cal aérea hidratada",
    ],
    [
        "Arena gruesa",
        6,
        "m3",
        "Arena gruesa lavada para hormigón y mampostería, entrega a granel",
        None,
    ],
    [
        "Hierro ADN 420 8 mm",
        150,
        "barra 12 m",
        "Barra de acero conformado ADN 420, diámetro 8 mm, largo 12 m, IRAM-IAS U 500-528",
        "Barra 8 mm en rollo",
    ],
]

#: Sin la columna accepted_alternatives, con "veinte" en la fila 3 y el ladrillo
#: repetido en la fila 5. Fila 2 y 4 son válidas.
CON_ERRORES_HEADERS = ["item", "quantity", "unit", "specification"]
CON_ERRORES = [
    ["Ladrillo hueco portante 18x19x33", 1200, "un", "Ladrillo cerámico hueco portante"],
    ["Cemento CPN40", "veinte", "bolsa 50 kg", "Cemento portland normal CPN40"],
    ["Cal hidráulica", 40, "bolsa 25 kg", "Cal hidráulica hidratada"],
    ["ladrillo hueco portante 18x19x33 ", 300, "un", "Repetido con otra capitalización"],
]


def write_workbook(path: Path, headers: list[str], rows: list[list]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "materiales"
    sheet.append(headers)

    for row in rows:
        sheet.append(row)

    workbook.save(path)


def main() -> None:
    write_workbook(HERE / "materiales_mamposteria.xlsx", HEADERS, MAMPOSTERIA)
    write_workbook(HERE / "materiales_con_errores.xlsx", CON_ERRORES_HEADERS, CON_ERRORES)
    print("escritos:", HERE / "materiales_mamposteria.xlsx", HERE / "materiales_con_errores.xlsx")


if __name__ == "__main__":
    main()
