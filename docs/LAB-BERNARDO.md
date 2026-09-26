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
