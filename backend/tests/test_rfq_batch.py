"""Lab L2: intake de Excel, batch de RFQs, un mail por proveedor.

Corre solo con SQLite, como toda la suite (ver conftest). El proveedor de mail es
``console``, así que "mandar" es loguear; lo que se verifica son las filas
``FollowUp`` que deja el envío, que es lo que el dashboard de la base también mira.
"""

import io
from datetime import date
from pathlib import Path
from urllib.parse import quote

import pytest
from openpyxl import Workbook

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.features.auth.model import User
from app.features.followup.model import FollowUp
from app.features.invitation.model import Invitation
from app.features.rfq.batch_invite import invite_suppliers_to_batch
from app.features.rfq.batch_model import RFQBatch
from app.features.rfq.batch_service import create_batch_from_rows
from app.features.rfq.intake import MaterialsIntakeError
from app.features.rfq.intake import parse_materials_xlsx
from app.features.rfq.model import RFQ
from app.features.supplier.model import Supplier

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
OK_FILE = EXAMPLES / "materiales_mamposteria.xlsx"
BAD_FILE = EXAMPLES / "materiales_con_errores.xlsx"

HEADERS = ["item", "quantity", "unit", "specification", "accepted_alternatives"]
WA_NUMBER = "5491122334455"
DELIVERY = date(2026, 10, 15)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ------------------------------------------------------------------ helpers
def _xlsx(headers: list[str], rows: list[list]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)

    for row in rows:
        sheet.append(row)

    buffer = io.BytesIO()
    workbook.save(buffer)

    return buffer.getvalue()


def _errors(file) -> list[str]:
    with pytest.raises(MaterialsIntakeError) as excinfo:
        parse_materials_xlsx(file)

    return excinfo.value.errors


def _register(client, email: str = "compras@constructora.example.com") -> dict:
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "clave-larga-y-segura",
            "full_name": "Diego Compras",
            "company_name": "Constructora Palermo SRL",
            "contact_email": email,
            "contact_phone": "+54 11 5555 0100",
        },
    )

    assert response.status_code == 201, response.text

    return response.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_supplier(client, token: str, name: str, email: str, contact: str | None = None) -> dict:
    response = client.post(
        "/suppliers",
        headers=_auth(token),
        json={"name": name, "contact_email": email, "contact_name": contact},
    )

    assert response.status_code in (200, 201), response.text

    return response.json()


BATCH_NAME = "Obra Palermo - mampostería"
SITE_NAME = "Edificio Palermo Soho"
SITE_ADDRESS = "Gorriti 4800, CABA"


def _create_batch(db, user):
    return create_batch_from_rows(
        db=db,
        user=user,
        rows=parse_materials_xlsx(OK_FILE),
        name=BATCH_NAME,
        site_name=SITE_NAME,
        site_address=SITE_ADDRESS,
        delivery_expectation=DELIVERY,
    )


def _import(client, token: str, file_path: Path, supplier_ids: list[int], **overrides):
    data = {
        "name": BATCH_NAME,
        "site_name": SITE_NAME,
        "site_address": SITE_ADDRESS,
        "delivery_expectation": DELIVERY.isoformat(),
        **overrides,
    }

    if supplier_ids:
        # httpx repite el campo por cada elemento de la lista: supplier_ids=3&supplier_ids=5
        data["supplier_ids"] = [str(supplier_id) for supplier_id in supplier_ids]

    with open(file_path, "rb") as handle:
        return client.post(
            "/rfq-batches/import",
            headers=_auth(token),
            data=data,
            files={"file": (file_path.name, handle, XLSX_MIME)},
        )


@pytest.fixture
def buyer(client, db_session):
    data = _register(client)
    user = db_session.get(User, data["user"]["id"])

    return {"token": data["access_token"], "user": user}


@pytest.fixture
def two_suppliers(client, buyer):
    first = _create_supplier(
        client, buyer["token"], "Corralón Norte", "ventas@corralonnorte.example.com", "Marta Pérez"
    )
    second = _create_supplier(
        client, buyer["token"], "Hierros del Sur", "cotizaciones@hierrosdelsur.example.com"
    )

    return [first, second]


# ------------------------------------------------------------------- parseo
def test_parse_example_file_ok():
    rows = parse_materials_xlsx(OK_FILE)

    assert len(rows) == 5
    assert [row["item"] for row in rows] == [
        "Ladrillo hueco portante 18x19x33",
        "Cemento CPN40",
        "Cal hidráulica",
        "Arena gruesa",
        "Hierro ADN 420 8 mm",
    ]
    assert [row["quantity"] for row in rows] == [1200, 60, 40, 6, 150]
    assert rows[0]["unit"] == "un"
    assert rows[3]["accepted_alternatives"] is None
    assert rows[4]["accepted_alternatives"] == "Barra 8 mm en rollo"
    assert [row["row"] for row in rows] == [2, 3, 4, 5, 6]


def test_parse_accepts_bytes_and_ignores_blank_rows():
    payload = _xlsx(HEADERS, [["Cemento", 10, "bolsa", "CPN40", None], [None, None, None, None, None]])

    rows = parse_materials_xlsx(payload)

    assert len(rows) == 1
    assert rows[0]["quantity"] == 10
    assert rows[0]["specification"] == "CPN40"


def test_parse_missing_header():
    errors = _errors(
        _xlsx(["item", "quantity", "specification", "accepted_alternatives"], [["Cal", 1, "x", None]])
    )

    assert len(errors) == 1
    assert errors[0].startswith("fila 1: faltan los encabezados 'unit'")


def test_parse_unknown_header():
    errors = _errors(_xlsx(HEADERS + ["precio"], [["Cal", 1, "bolsa", "", None, 100]]))

    assert errors == ["fila 1: columna desconocida 'precio'"]


def test_parse_quantity_text():
    errors = _errors(_xlsx(HEADERS, [["Cemento", "veinte", "bolsa", "", None]]))

    assert errors == ["fila 2: quantity debe ser numérica (recibido 'veinte')"]


@pytest.mark.parametrize("quantity", [0, -3])
def test_parse_quantity_not_positive(quantity):
    errors = _errors(_xlsx(HEADERS, [["Cemento", quantity, "bolsa", "", None]]))

    assert errors == [f"fila 2: quantity debe ser mayor que 0 (recibido {quantity})"]


def test_parse_quantity_not_integer():
    errors = _errors(_xlsx(HEADERS, [["Arena", 2.5, "m3", "", None]]))

    assert len(errors) == 1
    assert errors[0].startswith("fila 2: quantity debe ser entera (recibido 2.5)")


def test_parse_quantity_as_numeric_text_is_accepted():
    rows = parse_materials_xlsx(_xlsx(HEADERS, [["Arena", "6", "m3", "", None]]))

    assert rows[0]["quantity"] == 6


def test_parse_empty_item():
    errors = _errors(_xlsx(HEADERS, [[None, 5, "bolsa", "", None]]))

    assert errors == ["fila 2: item vacío"]


def test_parse_empty_unit():
    errors = _errors(_xlsx(HEADERS, [["Cemento", 5, "   ", "", None]]))

    assert errors == ["fila 2: unit vacío"]


def test_parse_duplicate_item_ignores_case_and_spaces():
    errors = _errors(
        _xlsx(
            HEADERS,
            [
                ["Cemento CPN40", 5, "bolsa", "", None],
                ["Cal", 5, "bolsa", "", None],
                ["  cemento   cpn40 ", 8, "bolsa", "", None],
            ],
        )
    )

    assert errors == ["fila 4: ítem duplicado 'cemento   cpn40' (ya aparece en la fila 2)"]


def test_parse_no_rows():
    errors = _errors(_xlsx(HEADERS, []))

    assert errors == ["El Excel no tiene filas de materiales debajo del encabezado."]


def test_parse_rejects_non_xlsx_bytes():
    errors = _errors(b"esto no es un excel")

    assert errors == ["El archivo no es un .xlsx válido."]


def test_error_example_reports_the_three_problems_at_once():
    errors = _errors(BAD_FILE)

    assert len(errors) == 3
    assert errors[0].startswith("fila 1: faltan los encabezados 'accepted_alternatives'")
    assert errors[1] == "fila 3: quantity debe ser numérica (recibido 'veinte')"
    assert errors[2].startswith("fila 5: ítem duplicado 'ladrillo hueco portante 18x19x33'")
    assert errors[2].endswith("(ya aparece en la fila 2)")


# -------------------------------------------------------------------- batch
def test_create_batch_creates_five_rfqs(db_session, buyer):
    rows = parse_materials_xlsx(OK_FILE)

    batch = _create_batch(db_session, buyer["user"])

    assert batch.id is not None
    assert batch.user_id == buyer["user"].id
    assert batch.status == "open"
    assert batch.delivery_expectation == DELIVERY

    rfqs = sorted(batch.rfqs, key=lambda rfq: rfq.id)

    assert len(rfqs) == 5
    assert db_session.query(RFQ).filter(RFQ.rfq_batch_id == batch.id).count() == 5

    hours_ahead = (batch.deadline - batch.created_at).total_seconds() / 3600
    assert 71.9 < hours_ahead < 72.1

    for rfq, row in zip(rfqs, rows, strict=True):
        # Mismo formato que genera la base: RFQ-<año>-<8 hex>.
        assert rfq.rfq_number.startswith("RFQ-2026-")
        assert len(rfq.rfq_number) == len("RFQ-2026-8F3A21C4")
        assert rfq.item_name == row["item"]
        assert rfq.quantity == row["quantity"]
        assert rfq.unit == row["unit"]
        assert rfq.specification
        assert rfq.currency == "ARS"
        assert rfq.status == "open"
        assert rfq.procurement_type == "goods"
        assert rfq.buyer_company == "Constructora Palermo SRL"
        assert rfq.site_name == SITE_NAME
        assert rfq.site_address == SITE_ADDRESS
        assert rfq.delivery_expectation == DELIVERY
        assert rfq.deadline == batch.deadline
        assert rfq.user_id == buyer["user"].id

    assert len({rfq.rfq_number for rfq in rfqs}) == 5
    assert rfqs[3].notes is None  # arena: sin alternativas
    assert rfqs[4].notes == "Alternativas aceptadas: Barra 8 mm en rollo"


# --------------------------------------------------------------- invitación
def test_invite_sends_one_email_per_supplier_with_five_links(
    db_session, buyer, two_suppliers, monkeypatch
):
    monkeypatch.setattr(settings, "BERNARDO_WA_NUMBER", WA_NUMBER)

    batch = _create_batch(db_session, buyer["user"])
    supplier_ids = [supplier["id"] for supplier in two_suppliers]

    result = invite_suppliers_to_batch(db=db_session, batch=batch, supplier_ids=supplier_ids)

    # 5 RFQs x 2 proveedores = 10 invitaciones, todas con token propio.
    assert len(result.invitations) == 10
    assert len({invitation.token for invitation in result.invitations}) == 10
    assert db_session.query(Invitation).count() == 10

    # UN mail por proveedor.
    assert len(result.emails) == 2
    assert [email.status for email in result.emails] == ["sent", "sent"]
    assert all(email.item_count == 5 for email in result.emails)

    followups = db_session.query(FollowUp).all()
    assert len(followups) == 10  # una fila de log por invitación, como la base
    bodies = {followup.body for followup in followups}
    assert len(bodies) == 2  # pero solo dos cuerpos distintos: dos mails

    for supplier in two_suppliers:
        own = [inv for inv in result.invitations if inv.supplier_id == supplier["id"]]
        body = next(f.body for f in followups if f.supplier_id == supplier["id"])

        assert body.count("Cotizar: ") == 5

        for invitation in own:
            assert settings.public_form_link(invitation.rfq_id, invitation.token) in body
            assert invitation.sent_at is not None

        expected_text = f"Hola Bernardo, soy {supplier['name']}. Mandame el pedido {batch.name}."
        expected_link = f"https://wa.me/{WA_NUMBER}?text={quote(expected_text, safe='')}"

        assert expected_link in body
        assert expected_link.startswith(f"https://wa.me/{WA_NUMBER}?text=Hola%20Bernardo%2C%20soy%20")

        outcome = next(e for e in result.emails if e.supplier_id == supplier["id"])
        assert outcome.whatsapp_link == expected_link
        assert outcome.to_email == supplier["contact_email"]

        db_supplier = db_session.get(Supplier, supplier["id"])
        assert db_supplier.last_contacted_at is not None

    # Copy: Bernardo firma, obra y entrega presentes, sin exclamaciones.
    body = next(iter(bodies))
    assert "Soy Bernardo, asistente de compras de Constructora Palermo SRL" in body
    assert SITE_NAME in body
    assert SITE_ADDRESS in body
    assert "Entrega esperada:   15/10/2026" in body
    assert "Ladrillo hueco portante 18x19x33: 1.200 un" in body
    assert "Alternativas aceptadas: Barra 8 mm en rollo" in body
    assert "!" not in body

    marta = next(f.body for f in followups if f.to_email == "ventas@corralonnorte.example.com")
    assert marta.startswith("Hola Marta,")

    assert followups[0].subject == f"Pedido de cotización: {batch.name} (5 ítems)"


def test_invite_without_whatsapp_number_omits_link_and_warns(
    db_session, buyer, two_suppliers, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "BERNARDO_WA_NUMBER", "")

    batch = _create_batch(db_session, buyer["user"])

    with caplog.at_level("WARNING"):
        result = invite_suppliers_to_batch(
            db=db_session, batch=batch, supplier_ids=[two_suppliers[0]["id"]]
        )

    assert len(result.emails) == 1
    assert result.emails[0].whatsapp_link is None
    assert "wa.me" not in db_session.query(FollowUp).first().body
    assert any("BERNARDO_WA_NUMBER" in record.message for record in caplog.records)


def test_invite_rejects_a_supplier_of_another_buyer(client, db_session, buyer, two_suppliers):
    other = _register(client, "otro@empresa.example.com")
    foreign = _create_supplier(client, other["access_token"], "Ajeno SA", "x@ajeno.example.com")

    batch = _create_batch(db_session, buyer["user"])

    with pytest.raises(NotFoundError):
        invite_suppliers_to_batch(db=db_session, batch=batch, supplier_ids=[foreign["id"]])

    assert db_session.query(Invitation).count() == 0


# ------------------------------------------------------------------ endpoint
def test_import_endpoint_creates_batch_invitations_and_emails(
    client, db_session, buyer, two_suppliers, monkeypatch
):
    monkeypatch.setattr(settings, "BERNARDO_WA_NUMBER", WA_NUMBER)

    supplier_ids = [supplier["id"] for supplier in two_suppliers]

    response = _import(client, buyer["token"], OK_FILE, supplier_ids)

    assert response.status_code == 201, response.text
    body = response.json()

    assert body["name"] == BATCH_NAME
    assert body["user_id"] == buyer["user"].id
    assert len(body["rfq_ids"]) == 5
    assert len(body["items"]) == 5
    assert body["items"][0]["item_name"] == "Ladrillo hueco portante 18x19x33"
    assert body["items"][0]["currency"] == "ARS"
    assert len(body["invitations"]) == 10
    assert all(inv["form_link"].startswith(settings.PUBLIC_FORM_URL) for inv in body["invitations"])
    assert len(body["emails"]) == 2
    assert all(email["status"] == "sent" for email in body["emails"])
    assert all(
        email["whatsapp_link"].startswith(f"https://wa.me/{WA_NUMBER}?text=") for email in body["emails"]
    )

    # GET devuelve lo mismo, sin la sección de mails.
    fetched = client.get(f"/rfq-batches/{body['id']}", headers=_auth(buyer["token"]))

    assert fetched.status_code == 200
    assert fetched.json()["rfq_ids"] == body["rfq_ids"]
    assert len(fetched.json()["invitations"]) == 10
    assert fetched.json()["emails"] == []

    # Los RFQs se ven por la API de la base, con su contador de invitaciones.
    rfq = client.get(f"/rfqs/{body['rfq_ids'][0]}", headers=_auth(buyer["token"])).json()
    assert rfq["invitation_count"] == 2
    assert rfq["procurement_type"] == "goods"

    # Otro comprador no lo ve.
    other = _register(client, "otro@empresa.example.com")
    assert (
        client.get(f"/rfq-batches/{body['id']}", headers=_auth(other["access_token"])).status_code
        == 404
    )


def test_import_endpoint_without_suppliers_creates_only_the_batch(client, db_session, buyer):
    response = _import(client, buyer["token"], OK_FILE, [])

    assert response.status_code == 201, response.text
    assert len(response.json()["rfq_ids"]) == 5
    assert response.json()["invitations"] == []
    assert response.json()["emails"] == []
    assert db_session.query(Invitation).count() == 0


def test_import_endpoint_reports_excel_errors_with_row_numbers(client, db_session, buyer):
    response = _import(client, buyer["token"], BAD_FILE, [])

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "fila 1: faltan los encabezados 'accepted_alternatives'" in detail
    assert "fila 3: quantity debe ser numérica (recibido 'veinte')" in detail
    assert "fila 5: ítem duplicado" in detail
    assert db_session.query(RFQBatch).count() == 0


def test_import_endpoint_rejects_foreign_supplier_before_creating_anything(
    client, db_session, buyer, two_suppliers
):
    other = _register(client, "otro@empresa.example.com")
    foreign = _create_supplier(client, other["access_token"], "Ajeno SA", "x@ajeno.example.com")

    response = _import(client, buyer["token"], OK_FILE, [two_suppliers[0]["id"], foreign["id"]])

    assert response.status_code == 404, response.text
    assert db_session.query(RFQBatch).count() == 0
    assert db_session.query(RFQ).count() == 0


def test_import_endpoint_rejects_non_xlsx(client, buyer, tmp_path):
    csv_file = tmp_path / "materiales.csv"
    csv_file.write_text("item,quantity\nCemento,10\n", encoding="utf-8")

    response = _import(client, buyer["token"], csv_file, [])

    assert response.status_code == 400, response.text
    assert ".xlsx" in response.json()["detail"]


def test_import_endpoint_requires_auth(client):
    with open(OK_FILE, "rb") as handle:
        response = client.post(
            "/rfq-batches/import",
            data={"name": "x", "delivery_expectation": "2026-10-15"},
            files={"file": (OK_FILE.name, handle, XLSX_MIME)},
        )

    assert response.status_code == 401
