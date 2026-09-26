# LAB-BERNARDO — laboratorio del agente cotizador

Fork descartable de `supplier-quote-autopilot` (MIT). Sirve para experimentar con el
agente cotizador de Bernardo. **Nunca se mergea al repo dashboard.**

## Lote L0 — setup y verificación (2026-09-26)

### Toolchain verificado

| Herramienta | Versión | Nota |
| --- | --- | --- |
| Python (sistema) | 3.14.4 | cumple `requires-python >= 3.13` |
| Python (venv del backend) | 3.13.15 | uv lo bajó solo por `backend/.python-version = 3.13` |
| uv | 0.12.19 | no estaba; instalado con `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` → `C:\Users\DELL\.local\bin` |
| Node | 24.15.0 | |
| npm | 11.12.1 | |

> Después de instalar uv hay que abrir una consola nueva (o anteponer
> `C:\Users\DELL\.local\bin` al PATH) para que `dev.cmd` lo encuentre.

### Instalación

- `backend/`: `uv sync --all-groups` → 78 paquetes, sin errores.
- `frontend/`: `npm install` → 293 paquetes. npm audit reporta 10 vulnerabilidades (1 low, 2 moderate, 7 high); no se tocaron.
- `public_form/`: `npm install` → 168 paquetes, 0 vulnerabilidades.

### Tests del backend (SQLite)

```
cd backend
uv run pytest -q
452 passed, 1 warning in 62.43s
```

- **452 pasados / 0 fallidos.** El README dice 424; el conteo real es mayor (hay un
  `test_demo.py` que el README no lista).
- Warning único: `StarletteDeprecationWarning` (httpx + starlette.testclient). No afecta.

### dev.cmd

| Servicio | Puerto | Verificado |
| --- | --- | --- |
| FastAPI + Swagger | 8000 | `GET /docs` 200, `GET /health` 200 (`database.connected: true`) |
| Dashboard comprador (Vite) | 5173 | 200 |
| Formulario proveedor (Vite) | 5174 | 200 |

`dev.cmd stop` liberó los tres puertos.

**Hallazgo (no corregido, por regla del lote):** en el primer `dev.cmd`, el paso
`[6/6]` corre `scripts.seed_demo` y falla con
`sqlite3.OperationalError: unable to open database file`. La causa es que
`backend/var/` no existe todavía (está en `.gitignore`) y ni `seed_demo.py` ni
`DATABASE_URL=sqlite:///./var/dev.db` la crean; recién la crea `app/core/storage.py`
al arrancar la API (`self.root.mkdir(parents=True)` para `./var/uploads`). Consecuencia:
la API arranca bien pero **sin demo cargada**, y en los runs siguientes `dev.cmd` ve
`var/dev.db` (quedó de 0 bytes tras el stop) y no vuelve a intentar el seed. Workaround para el próximo lote:
`dev.cmd seed` (ahora que `var/` existe) o `dev.cmd reset`.

### Archivos generados localmente (todos ignorados por git)

`backend/.env` (copia de `.env.dev`), `frontend/.env`, `public_form/.env`,
`backend/.venv/`, `backend/var/`, `frontend/node_modules/`, `public_form/node_modules/`.

### Cambios a archivos de la base en este lote

Solo `.gitignore`: se agregó `prompts/` (pedido explícito de la tarea 7). Nada más.

## Mapa del repo (referencia para lotes siguientes)

```
backend/app/features/        un paquete por dominio: model.py, schema.py, service.py, router.py
  rfq/                       RFQ + taxonomy.py (tipos de compra goods/service)
  supplier/ · invitation/    proveedores y links tokenizados (invitation/messaging.py arma el mail)
  public_form/               superficie sin auth: el proveedor cotiza por su link
  quote/                     cotizaciones; importers/ (csv_importer.py, pdf_importer.py)
  comparison/                orquesta comparison/ + narrative.py (resumen LLM o fallback)
  followup/                  scheduler.py + service.py: chasing con aprobación humana
  attachment/ · auth/ · chat/ · dashboard/ · demo/ (workspace.json = demo read-only)
backend/app/core/            config.py, database.py, llm_client.py, email.py, storage.py, rate_limit.py
backend/app/ai/              completer.py / llm.py / registry.py + agents/ LangChain heredados
comparison/                  motor puro, sin DB: engine.py, cost.py, score.py, fx.py, units.py,
                             incoterms.py, recommend.py, exporters.py (CSV), schemas.py
agents/quote_parser/         parser.py (capas), heuristic.py, normalize.py, classify.py,
                             completeness.py (reglas "TBD no es respuesta"), prompts.py, schemas.py
agents/followup/             policy.py (orden de decisión), drafter.py, prompts.py, schemas.py
backend/tests/               16 archivos; conftest.py levanta SQLite temporal, sin LLM ni mail real
```

Comandos útiles: `uv run pytest -q` · `uv run ruff check . ../agents ../comparison` ·
`uv run python -m scripts.list_routes` · `dev.cmd [start|seed|reset|links|stop]`.

## Lote L1 — schema (2026-09-26)

### Base de datos

`backend/.env` apunta al **Session pooler de Supabase**, proyecto `ugxdryhyucwdzgaqkwtd`
(bernardo-cotizador-lab, `sa-east-1`, Postgres 17.6). Host correcto:
`aws-0-sa-east-1.pooler.supabase.com:5432`, usuario `postgres.<ref>`. Con `aws-1` el pooler
responde `FATAL: (ENOTFOUND) tenant/user ... not found`: no es error de contraseña, es que
ese cluster no tiene el tenant. `DATABASE_URL_DIRECT` es la misma URL (Alembic la usa).

Estado tras `uv run alembic upgrade head`: `alembic_version = 80133b065ad3`.

### Regla: los tests NUNCA se corren contra Supabase

`tests/conftest.py` fija `DATABASE_URL` a un SQLite temporal y, en cada test, hace
`Base.metadata.drop_all()` + `create_all()`. Contra Supabase eso destruiría el schema
migrado (y de paso fallaría en el primer `DROP TABLE supplier_quotes` porque la vista
`v_price_history` depende de ella). **La suite se corre solo con SQLite, como hace la
base.** No se crean schemas auxiliares ni plugins para desviar el conftest.

### Tabla nueva: `rfq_batches` (`backend/app/features/rfq/batch_model.py`, 105 líneas)

La base modela un RFQ = un ítem; el lab agrupa N ítems en un batch.

| Columna | Tipo | Notas |
| --- | --- | --- |
| id | integer PK | índice |
| name | varchar(255) NOT NULL | |
| site_name / site_address | varchar(255) / varchar(1000) | nullable |
| delivery_expectation | date | nullable |
| deadline | timestamptz NOT NULL | default Python `now + 72 h`, índice |
| status | varchar(16) NOT NULL | `open` (default) · `closed` · `expired`, índice |
| notes | varchar(2000) | nullable |
| created_at / updated_at | timestamptz | `TimestampMixin` |

Relación `RFQBatch.rfqs` ↔ `RFQ.batch`, sin cascade. Registrado en `app/models.py`.
Sin `user_id` (no estaba en el spec): si el dashboard filtra por comprador, agregarlo en
un lote siguiente.

### Columnas nuevas (todas nullable)

| Tabla.columna | Tipo | Notas |
| --- | --- | --- |
| rfqs.rfq_batch_id | integer FK → rfq_batches.id | `ON DELETE SET NULL`, índice, constraint `fk_rfqs_rfq_batch_id_rfq_batches` |
| suppliers.whatsapp_phone | varchar(64) | |
| suppliers.rubros | json | lista, p. ej. `["hierro","cemento"]` |
| suppliers.last_contacted_at | timestamptz | |
| suppliers.quoted_count | integer | default 0 |
| suppliers.awarded_count | integer | default 0 |
| supplier_quotes.iva_included | boolean | NULL = el proveedor no lo aclaró |
| supplier_quotes.freight_included | boolean | idem |

### Vista `v_price_history`

`supplier_quotes sq JOIN rfqs r LEFT JOIN suppliers s`. Columnas: supplier_id,
supplier_name (`COALESCE(s.name, sq.supplier_name)`), rfq_id, item_name, unit_price,
currency, iva_included, freight_included, payment_terms, lead_time, validity_date, source,
submitted_at. LEFT JOIN porque `supplier_quotes.supplier_id` es nullable. No está en
`Base.metadata`: solo existe donde corrió Alembic (Supabase), no en el SQLite de los tests.

### Migración `20260926_1539_80133b065ad3_lab_l1_rfq_batches_and_supplier_fields.py`

Autogenerada contra un SQLite temporal migrado a head (el mismo método del paso
`alembic check` del CI), porque en ese momento Supabase no conectaba. Retoques a mano: nombre
de la FK (autogenerate la dejó `None` y el downgrade no podía borrarla) y la vista con
`op.execute`. Validada en SQLite: upgrade → downgrade → upgrade → `alembic check` sin drift.
El SQL offline para PostgreSQL renderiza limpio.

Procedimiento reutilizable para la próxima migración (no toca `.env`):

```powershell
$sq = "sqlite:///C:/ruta/temporal/autogen.db"
$env:DATABASE_URL = $sq; $env:DATABASE_URL_DIRECT = $sq   # las dos, o .env gana
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "nombre"
uv run alembic check
```

### Tests (SQLite)

```
uv run pytest -q
452 passed, 1 warning in 61.83s
```

Mismo resultado que L0. Ningún test de la base se rompió por las columnas nuevas.
`ruff check . ../agents ../comparison`: sin errores.

### Archivos de la base tocados en L1

`app/models.py`, `features/rfq/model.py`, `features/supplier/model.py`,
`features/quote/model.py` (columnas y registro). Nuevos: `features/rfq/batch_model.py`,
la migración, y esta sección.

## Lote L2 — intake y batch (2026-09-26)

Un Excel de formato fijo se convierte en un `rfq_batch` con un RFQ por fila, y cada
proveedor elegido recibe **un** mail por batch con la lista completa, sus links al
formulario de la base y un link `wa.me` para seguir por WhatsApp.

### Flujo

```
POST /rfq-batches/import (multipart)
  file .xlsx ─► intake.parse_materials_xlsx ─► rows (todos los errores de una vez, con fila)
  supplier_ids ─► batch_invite.resolve_suppliers (ajeno = 404, ANTES de crear nada)
  ─► batch_service.create_batch_from_rows ─► RFQBatch + N RFQ (rfq_batch_id, goods, ARS)
  ─► batch_invite.invite_suppliers_to_batch
        ─► InvitationService.bulk_create por RFQ (una Invitation por rfq × proveedor)
        ─► un EmailMessage por proveedor ─► send_email_message_sync (base)
        ─► un FollowUp kind=manual por invitación (mismo cuerpo), sent_at, last_contacted_at
GET  /rfq-batches/{id}   batch + items + invitations (emails viven en FollowUp)
```

### Archivos nuevos (`backend/app/features/rfq/`, todos < 400 líneas)

| Archivo | Líneas | Qué hace |
| --- | --- | --- |
| `intake.py` | 243 | `parse_materials_xlsx(file)`: hoja 1, encabezados exactos `item, quantity, unit, specification, accepted_alternatives`; valida y devuelve dicts |
| `batch_service.py` | 146 | `create_batch_from_rows(...)`, `get_batch`, `list_batch_invitations` |
| `batch_invite.py` | 341 | `invite_suppliers_to_batch`, `resolve_suppliers`, `build_batch_email`, `whatsapp_link` |
| `batch_schema.py` | 136 | `RFQBatchResponse` y `build_batch_response` |
| `batch_router.py` | 120 | `POST /rfq-batches/import`, `GET /rfq-batches/{id}` |
| `backend/examples/make_examples.py` | 94 | genera los dos Excel de ejemplo |
| `backend/tests/test_rfq_batch.py` | ~500 | 25 tests |

Archivos de la base tocados, solo registro: `main.py` (import, `include_router`, tag),
`core/config.py` (`BERNARDO_WA_NUMBER`), `.env.example` (bloque "Lab Bernardo — WhatsApp"),
`rfq/batch_model.py` (columna `user_id` + relación `owner`), `pyproject.toml` / `uv.lock` /
`requirements.txt` (openpyxl 3.1.5; el CI compara requirements.txt con el lock).

### Migración `20260926_1614_25a559685695_lab_l2_rfq_batches_user_id.py`

`rfq_batches.user_id` integer **nullable**, FK `users.id ON DELETE CASCADE`
(`fk_rfq_batches_user_id_users`), índice. Aplicada en Supabase: `alembic_version =
25a559685695`.

**Pendiente declarado:** pasa a `NOT NULL` en una migración posterior. `batch_service` ya
exige `user_id` en toda fila nueva; cuando haya que limpiar: `UPDATE rfq_batches SET user_id
= <buyer> WHERE user_id IS NULL` (o borrar esas filas) y después `ALTER COLUMN user_id SET
NOT NULL`. Hoy no hay filas huérfanas (la tabla nació vacía en L1).

### Decisiones fuera del spec

- **`quantity` debe ser entera**, además de numérica y > 0: `rfqs.quantity` es `Integer` en
  la base. El error dice la fila y sugiere cambiar la unidad; no se redondea en silencio.
- **`procurement_type="goods"`** para los RFQs del batch. El default de la base es
  `service` y le pediría al corralón SLA de atención y matrículas.
- **Encabezados estrictos**: falta uno → error; columna extra → error. Formato fijo es fijo.
- **Todos los errores del Excel en una pasada**, no el primero.
- `specification` vacía cae al nombre del ítem (columna NOT NULL); `accepted_alternatives`
  va a `rfqs.notes` con prefijo "Alternativas aceptadas:".
- El multipart lleva `supplier_ids` **repetido** (`supplier_ids=3&supplier_ids=4`), no
  `supplier_ids[]`: es lo que mandan Swagger UI y httpx y lo que lee FastAPI.
- `BERNARDO_WA_NUMBER` se normaliza a dígitos (se tolera un "+" o espacios). Vacío → el
  mail sale sin link y se loguea un warning. Texto del link:
  `Hola Bernardo, soy {supplier.name}. Mandame el pedido {batch.name}.`
- El mail lo firma "Bernardo, asistente de compras de {company_name}" del comprador. Plazo
  en hora de Buenos Aires. Sin exclamaciones ni emojis (hay test).

### Tests (SQLite)

```
uv run pytest tests/test_rfq_batch.py -q   -> 25 passed
uv run pytest -q                           -> 477 passed, 1 warning in 55.36s
uv run ruff check . ../agents ../comparison -> All checks passed
```

Cubren: parseo OK, cada validación, batch con 5 RFQs (rfq_number con el generador de la
base, deadline 72 h), 10 invitaciones y 2 mails con 5 links cada uno, `wa.me` exacto,
warning sin número, proveedor ajeno → 404 sin crear nada, endpoint end-to-end, 400 con
filas para el Excel roto, 400 no-xlsx, 401 sin auth.

### Prueba manual contra Supabase (2026-09-26)

API local con `uvicorn` apuntando a Supabase, comprador demo del seed
(`buyer@demo-autopilot.example.com`), proveedores 3 y 4 del seed, Excel
`examples/materiales_mamposteria.xlsx`. Hecha vía la API HTTP (mismo endpoint que expone
`/docs`).

| Qué | Resultado |
| --- | --- |
| Import | 201 |
| Batch | id 1 |
| RFQs | ids 2, 3, 4, 5, 6 |
| Invitaciones | ids 5 a 14 (5 por proveedor) |
| Mails | 2, status `sent`, con `whatsapp_link` |
| GET /rfq-batches/1 | 200, 5 rfq_ids, 10 invitaciones |

El mail completo tal como salió en consola está en `prompts/2026-09-26-l2-intake.txt`.

### Para L3

- `NOT NULL` en `rfq_batches.user_id` cuando corresponda.
- El formulario público de la base es por RFQ: el proveedor abre 5 links. Un formulario por
  batch es el siguiente salto de UX.
- `iva_included` / `freight_included` (L1) aún no se piden en el formulario público ni se
  parsean: el mail ya los pide en texto, el schema del form no.
- La demo y el batch 1 quedaron en Supabase; `dev.cmd reset` borraría el SQLite local, no eso.
