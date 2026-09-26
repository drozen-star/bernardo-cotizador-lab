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
