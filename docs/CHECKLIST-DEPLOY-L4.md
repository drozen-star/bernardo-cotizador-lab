# CHECKLIST DE DEPLOY (L4)

**Fecha**: 2026-09-26
**Estado**: vigente para L4
**Extiende**: `SPEC-LAB-COTIZADOR-2026-09-26.md` sección 3 y `HANDOFF-LAB-COTIZADOR-L4-2026-09-26.md` sección 4
**Ítem de roadmap**: Cotizador / Lab agente v0

Preparado, no ejecutado. `render.yaml` provisiona solo el web service del backend, plan
starter, sin base de Render, sin frontend y sin migraciones ni seeds en el start command.

## 0. Antes del deploy

1. Aplicar a mano la migración L4 en Supabase (no la aplica el deploy):
   ```sql
   ALTER TABLE whatsapp_conversations ADD COLUMN wa_from VARCHAR(32);
   DROP INDEX ix_whatsapp_messages_wa_message_id;
   CREATE UNIQUE INDEX ix_whatsapp_messages_wa_message_id ON whatsapp_messages (wa_message_id);
   UPDATE alembic_version SET version_num = 'ce9f23959dc1' WHERE version_num = '1690dd87a51a';
   ```
   O desde `backend/` con el `.env` apuntando a Supabase: `uv run alembic upgrade head`.
2. Cargar `suppliers.whatsapp_phone` del proveedor de prueba (no hay campo en la UI ni en la API
   de la base; va por SQL o por el dashboard de Supabase). Cualquier formato: se normaliza.
3. Tener un batch abierto con ese proveedor invitado (L2: `POST /rfq-batches/import`).

## 1. Variables de entorno en Render

Dos grupos, igual que en `render.yaml`. **`SECRET_KEY` fuerte y distinto de cualquier otro
servicio: la API de la base queda pública en Render.**

### 1a. A cargar a mano en el dashboard (`sync: false`)

| Variable | Valor | Notas |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://postgres.<ref>:<pass>@aws-0-sa-east-1.pooler.supabase.com:5432/postgres` | Session pooler (5432). `aws-0`, no `aws-1`. |
| `DATABASE_URL_DIRECT` | misma URL | La usa Alembic; no hace falta si no se migra desde Render. |
| `SECRET_KEY` | secreto largo, único | JWT del dashboard. Con el default la app loguea error. |
| `SCHEDULER_SECRET` | secreto largo | Habilita `POST /internal/scheduler/tick`. |
| `ANTHROPIC_API_KEY` | key de Anthropic | Agente conversador. Nunca en el repo. |
| `LLM_API_KEY` | key de Anthropic (o vacía) | Funciones de la base por la capa OpenAI-compat; vacía = fallbacks. |
| `WHATSAPP_TOKEN` | token de Cloud API | Del System User de la app de Meta. |
| `WHATSAPP_PHONE_NUMBER_ID` | id del número | El del número de Bernardo (compartido con el bot). |
| `LAB_SHARED_SECRET` | secreto largo | El bot lo manda en `X-Bernardo-Lab-Secret`. |
| `LAB_ADMIN_TOKEN` | otro secreto largo | Para aprobar borradores. Distinto del anterior. |
| `BACKEND_URL` | URL pública de Render | Se conoce después del deploy. |
| `FRONTEND_URL` / `PUBLIC_FORM_URL` | URLs de los frontends (o la del backend si no hay) | El link del formulario en los mails sale de `PUBLIC_FORM_URL`. |
| `ALLOWED_ORIGINS` | `*` al principio, después los orígenes exactos | Vacío rompe CORS. |

### 1b. Fijas en `render.yaml` (con su valor escrito)

| Variable | Valor | Por qué |
| --- | --- | --- |
| `ENV` | `production` | Activa los warnings de configuración. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `10080` | Default de la base. |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_RECYCLE_SECONDS` | `5` / `5` / `300` | Pool para el pooler. |
| `BERNARDO_MODEL` | `claude-sonnet-5` | Agente conversador. |
| `WHATSAPP_MAX_TOKENS` / `WHATSAPP_MAX_TOOL_ROUNDS` | `4096` / `5` | Defaults del slice, explícitos. |
| `GRAPH_API_VERSION` | `v23.0` (el mismo que el bot; verificar si el bot lo tiene cargado en Render) | Sin default en el código: si falta, no se envía. |
| `WHATSAPP_SEND_TIMEOUT_SECONDS` / `WHATSAPP_WINDOW_HOURS` | `10` / `24` | Timeout de envío y ventana de Meta. |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_ENABLED` | `https://api.anthropic.com/v1/` / `claude-haiku-4-5-20251001` / `true` | Spec sección 7. |
| `EMAIL_PROVIDER` | `console` | En L4 no sale mail real. |
| `STORAGE_BACKEND` | `local` | Sin adjuntos en L4; solo warning en producción. |
| `SCHEDULER_ENABLED` / `SCHEDULER_INTERVAL_MINUTES` / `AUTO_SEND_FOLLOWUPS` | `true` / `15` / `false` | Recordatorios de la base con aprobación. |
| `DEFAULT_PROCUREMENT_TYPE` | `goods` | Materiales, no servicios. |
| `BASE_CURRENCY` / `DEFAULT_GST_RATE` | `ARS` / `0` | Argentina; el IVA va por flag en `supplier_quotes`. |
| `DEMO_MODE_ENABLED` | `false` | Sin demo pública en el lab. |

Fuera del yaml en L4 (la base no las necesita hoy): `EMAIL_API_KEY` y `MAIL_FROM` (mail en
console), `S3_*` y `MAX_UPLOAD_MB` (storage local), `CAPTCHA_*`, `FX_RATES_JSON`,
`DEFAULT_SCORING_WEIGHTS_JSON`, `BERNARDO_WA_NUMBER` (solo va en el mail de invitación de L2,
que en L4 no se manda). Se agregan cuando el mail pase a real.

Notas: `backend/.python-version` fija Python 3.13 (Render lo lee). El start command corre
`uvicorn app.main:app`; el arranque hace `create_all` (no altera tablas existentes) y prende
el scheduler de la base.

## 2. Orden del handoff

1. **Lab arriba**: deploy en Render, `GET /health` responde 200 con `database.connected: true`.
2. **Bot con el interruptor apagado**: configurar en el bot `LAB_BASE_URL` y `LAB_SHARED_SECRET`
   pero con la consulta al lab desactivada (`LAB_ENABLED=false` o equivalente).
3. **Prueba**: desde PowerShell, `POST /whatsapp/inbound` con el número del proveedor de
   prueba (ver contrato). Esperado: `{"owned": true, "reason": "opened_now"}` y un borrador en
   `GET /whatsapp/conversations/{id}/drafts`. Aprobar y verificar que el WhatsApp llega.
4. **Encender**: `LAB_ENABLED=true` en el bot. Primer mensaje real desde el teléfono del
   proveedor. Revisar logs del lab (`whatsapp.inbound owned=... reason=...`).

## 3. Aprobar un borrador (PowerShell, sin curl)

```powershell
$lab = "https://<servicio>.onrender.com"
$admin = @{ "X-Bernardo-Lab-Admin" = $env:LAB_ADMIN_TOKEN }

# Ver qué hay pendiente
Invoke-RestMethod -Method Get -Uri "$lab/whatsapp/conversations/1/drafts" -Headers $admin | ConvertTo-Json -Depth 5

# Aprobar el último borrador pendiente
Invoke-RestMethod -Method Post -Uri "$lab/whatsapp/conversations/1/approve" -Headers $admin -ContentType "application/json" -Body "{}"

# Aprobar uno puntual
Invoke-RestMethod -Method Post -Uri "$lab/whatsapp/conversations/1/approve" -Headers $admin -ContentType "application/json" -Body '{"message_id": 7}'
```

Respuestas: `200` mensaje enviado (con `wa_message_id` de Meta); `409 window_closed` si pasaron
más de 24 h del último mensaje del proveedor; `409 already_sent`; `404 no_draft`; `502
meta_send_failed:<code>` (el borrador sigue pendiente, reintentar).

## 4. Memoria en Render después del deploy

- Dashboard → servicio → **Metrics** → gráfico **Memory**: mirar el valor en reposo (después
  de 10 min sin tráfico) y el pico durante una conversación. El plan starter da 512 MB.
- Desde PowerShell, disparar carga y mirar el gráfico:
  ```powershell
  1..5 | ForEach-Object { Invoke-RestMethod -Uri "$lab/health" | Out-Null; Start-Sleep -Seconds 2 }
  ```
- Referencia local: `uvicorn` con la base y `anthropic` importados arranca en ~150 MB; el
  simulador no corre en Render (es local). Si el reposo supera ~350 MB, bajar `DB_POOL_SIZE`
  a 2 y revisar `LLM_MAX_CONCURRENCY`.
- Logs: Dashboard → **Logs**, filtrar `whatsapp.inbound` y `whatsapp.approve`.
