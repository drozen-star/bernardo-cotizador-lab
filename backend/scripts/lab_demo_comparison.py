"""Demo del comparativo por pedido (lab L5d), solo sobre SQLite.

    $env:DATABASE_URL = "sqlite:///var/lab_demo_comparison.db"
    $env:DATABASE_URL_DIRECT = $env:DATABASE_URL
    uv run python -m scripts.lab_demo_comparison

Si ``DATABASE_URL`` no empieza con ``sqlite`` el script frena con mensaje y código 1: nunca
escribe en Supabase. Crea el pedido "Demo mampostería" con cinco ítems y tres proveedores
que cubren los casos del modelo fiscal, genera el Excel en ``backend/var/`` e imprime la
ruta y las dos estrategias.
"""

import sys
from datetime import date
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

# Permite `python -m scripts.lab_demo_comparison` desde backend/.
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402

REFUSAL = "Este script solo corre sobre SQLite. DATABASE_URL apunta a otra base: no se toca."

BATCH_NAME = "Demo mampostería"

#: (ítem, cantidad, unidad, especificación)
ITEMS = [
    ("Ladrillo cerámico hueco 18x19x33", 1200, "un", "Ladrillo cerámico hueco portante 18x19x33 cm"),
    ("Bloque de hormigón 19x19x39", 400, "un", "Bloque de hormigón vibrado 19x19x39 cm"),
    ("Cemento CPN40 bolsa 50 kg", 60, "bolsa", "Cemento portland normal CPN40, bolsa de 50 kg"),
    ("Cal hidratada bolsa 25 kg", 40, "bolsa", "Cal hidratada en polvo, bolsa de 25 kg"),
    ("Arena gruesa", 6, "m3", "Arena gruesa lavada, a granel"),
]

#: Precios unitarios por proveedor; None = no cotiza el ítem. Los kwargs pisan el default.
SUPPLIERS = [
    {
        # Completo, con IVA incluido y flete incluido.
        "name": "Corralón Norte",
        "email": "ventas@corralonnorte.example.com",
        "prices": ["890", "1450", "12100", "6200", "44000"],
        "quote": dict(iva_included=True, freight_included=True, lead_time=3, payment_terms="contado"),
        "validity_days": 7,
    },
    {
        # Más barato, sin IVA, "flete aparte" sin costo informado, validez textual.
        "name": "Materiales del Sur",
        "email": "cotizaciones@materialesdelsur.example.com",
        "prices": ["690", "1130", "10200", "4900", "37500"],
        "quote": dict(iva_included=False, freight_included=False, lead_time=5, payment_terms="cuenta corriente 30 días"),
        "remarks": "Validez: 48 horas",
    },
    {
        # No cotiza la arena y no aclara IVA en la cal.
        "name": "Ferretería Oeste",
        "email": "pedidos@ferreteriaoeste.example.com",
        "prices": ["720", "1200", "10900", "4700", None],
        "quote": dict(iva_included=True, freight_included=True, lead_time=7, payment_terms="contado"),
        "validity_days": 10,
        "overrides": {3: dict(iva_included=None)},
    },
]


def refuse_unless_sqlite(database_url: str) -> None:
    if not (database_url or "").startswith("sqlite"):
        print(REFUSAL)
        raise SystemExit(1)


def _seed(db):
    from app.core.mixins import utcnow
    from app.features.auth.model import User
    from app.features.quote.model import SupplierQuote
    from app.features.rfq.batch_service import create_batch_from_rows
    from app.features.supplier.model import Supplier

    user = User(
        email="demo-comparativo@bernardo.example.com",
        hashed_password="sin-login",
        full_name="Demo comparativo",
        company_name="Constructora Demo",
    )
    db.add(user)
    db.flush()

    rows = [{"item": name, "quantity": qty, "unit": unit, "specification": spec} for name, qty, unit, spec in ITEMS]
    batch = create_batch_from_rows(
        db=db, user=user, rows=rows, name=BATCH_NAME, site_name="Obra Demo",
        site_address="Av. Siempreviva 742, CABA", delivery_expectation=date.today() + timedelta(days=21),
    )
    rfqs = sorted(batch.rfqs, key=lambda rfq: rfq.id)

    for spec in SUPPLIERS:
        supplier = Supplier(user_id=user.id, name=spec["name"], contact_email=spec["email"])
        db.add(supplier)
        db.flush()

        for index, (rfq, price) in enumerate(zip(rfqs, spec["prices"])):
            if price is None:
                continue

            fields = dict(spec["quote"])
            fields.update(spec.get("overrides", {}).get(index, {}))
            db.add(
                SupplierQuote(
                    rfq_id=rfq.id, supplier_id=supplier.id, supplier_name=supplier.name,
                    unit_price=Decimal(price), currency="ARS", unit=rfq.unit, source="manual",
                    submitted_at=utcnow(), remarks=spec.get("remarks"),
                    validity_date=(date.today() + timedelta(days=spec["validity_days"])) if spec.get("validity_days") else None,
                    **fields,
                )
            )

    db.commit()

    return batch


def main() -> int:
    refuse_unless_sqlite(settings.DATABASE_URL)

    from app import models  # noqa: F401 - registra todas las tablas
    from app.core.database import Base
    from app.core.database import SessionLocal
    from app.core.database import engine
    from app.features.batch_comparison import excel
    from app.features.batch_comparison import service

    Base.metadata.create_all(bind=engine)  # solo crea lo que falta; no borra nada

    with SessionLocal() as db:
        batch = _seed(db)
        result = service.build(db, batch.id)
        out_dir = BACKEND_DIR / "var"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / excel.filename_for(batch.name, result.generated_at)
        excel.build_workbook(result).save(path)

    print(f"Excel generado: {path}")
    print(f"Pedido: {batch.name} (batch {batch.id}, {len(ITEMS)} ítems, {len(SUPPLIERS)} proveedores)")

    for strategy in result.comparison.strategies:
        print()
        print(f"[{strategy.title}]")
        print(f"  costo real total: {service.fiscal.format_money(strategy.total_costo_real)}")
        print(f"  desembolso total: {service.fiscal.format_money(strategy.total_desembolso)}")
        print(f"  proveedores: {strategy.supplier_count} ({', '.join(strategy.supplier_names)})")
        print(f"  plazo máximo: {strategy.max_lead_time} días")

        for assignment in strategy.assignments:
            print(f"    - {assignment.item_name}: {assignment.supplier_name or 'sin cotización'}")

        print(f"  marcas: {'; '.join(strategy.marks) or 'ninguna'}")
        print(f"  porqué: {strategy.porque}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
