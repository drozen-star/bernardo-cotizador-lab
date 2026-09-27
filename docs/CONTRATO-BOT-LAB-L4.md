# CONTRATO BOT → LAB (L4)

**Fecha**: 2026-09-26
**Estado**: vigente para L4
**Extiende**: `SPEC-LAB-COTIZADOR-2026-09-26.md` sección 3 y `HANDOFF-LAB-COTIZADOR-L4-2026-09-26.md` sección 4
**Ítem de roadmap**: Cotizador / Lab agente v0

## Qué resuelve

El bot de Bernardo en Render recibe todos los mensajes de WhatsApp. Para cada mensaje
entrante le pregunta al lab "¿es tuyo?". Si el lab dice que sí, el bot no lo procesa (no lo
manda al bot de facturas ni a ningún otro camino). Si dice que no, sigue su camino normal.
El lab nunca responde al proveedor desde este endpoint: la respuesta la redacta el agente,
queda como borrador y la manda una persona con `POST /whatsapp/conversations/{id}/approve`.

## Endpoint

`POST {LAB_BASE_URL}/whatsapp/inbound`

### Autenticación

Header `X-Bernardo-Lab-Secret: <LAB_SHARED_SECRET>`. Se compara con `hmac.compare_digest`.
Header ausente, distinto o secreto no configurado en el lab → `401` sin detalle (`{"detail":
"Unauthorized"}`).

### Body (JSON)

```json
{
  "wa_message_id": "wamid.HBgN...",
  "from": "5491155551234",
  "timestamp": "1790000000",
  "type": "text",
  "text": "Hola Bernardo, soy Corralón Norte. Mandame el pedido Obra Palermo - mampostería."
}
```

| Campo | Tipo | Notas |
| --- | --- | --- |
| `wa_message_id` | string | id del mensaje de Meta. Clave del dedupe. |
| `from` | string | `wa_id` crudo de Meta, sin tocar. El lab lo guarda tal cual y es el destino de las respuestas. |
| `timestamp` | string | el de Meta, como llega. |
| `type` | string | `text`, `image`, `audio`, `document`, etc. |
| `text` | string o null | cuerpo del texto; null si no es texto. |

Body inválido → `422`.

### Respuesta `200`

```json
{ "owned": true, "reason": "opened_now" }
```

| `owned` | `reason` | Qué pasó en el lab |
| --- | --- | --- |
| true | `open_conversation` | Había una conversación abierta con ese proveedor. El mensaje se guardó y el agente corre en background. |
| true | `opened_now` | Primer mensaje del proveedor: se abrió la conversación con el batch al que estaba invitado y el agente responde con la lista completa (borrador). |
| true | `needs_human_hold` | La conversación espera a una persona. El mensaje se guardó; el agente no corre. |
| true | `duplicate` | Ese `wa_message_id` ya se había procesado. Nada más pasa. |
| false | `not_a_supplier` | El número no coincide con ningún `suppliers.whatsapp_phone`. |
| false | `no_active_conversation` | Proveedor conocido pero sin conversación abierta ni batch abierto al que esté invitado (la última conversación está en `complete`, `supplier_declined` o `expired`). |

Regla del bot: `owned: true` → no procesar. `owned: false` → camino normal.

### Tiempo y fallas

- El bot espera **como máximo 5 s**. El lab decide sin llamar al modelo, así que responde en
  milisegundos; el agente corre después, en background.
- Timeout o error (5xx, red): el bot **loguea y NO deriva al bot de facturas** (regla del
  handoff). Puede reintentar: el dedupe por `wa_message_id` hace el reintento inocuo.

### Normalización del teléfono

El lab compara el `from` contra `suppliers.whatsapp_phone` llevando ambos a **solo dígitos con
el 9 de celular**: `+54 9 11 5555-1234`, `541155551234` y `5491155551234` son el mismo número
(`5491155551234`). El `00` inicial se quita. Números no argentinos quedan como dígitos. El
valor crudo de `from` **no se modifica**: se guarda en `whatsapp_conversations.wa_from` y es el
`to` de todo envío.

### Tipos que no son texto

En una conversación abierta, un mensaje con `type != "text"` se guarda con
`guardrail_flags = ["unsupported_media"]` y cuerpo `[<type>]`, sin llamar al agente. La
respuesta es `owned: true` con el motivo que corresponda a la conversación.

### Qué loguea el lab

Por cada decisión: `owned`, `reason`, últimos 4 dígitos del teléfono, `wa_message_id`,
`conversation_id` y acción. **Nunca el cuerpo del mensaje.**

## Lado admin (no lo usa el bot)

- `GET /whatsapp/conversations/{id}/drafts` con header `X-Bernardo-Lab-Admin: <LAB_ADMIN_TOKEN>`:
  borradores pendientes, destino y si la ventana de 24 h está abierta.
- `POST /whatsapp/conversations/{id}/approve` mismo header, body opcional `{"message_id": N}`
  (sin body: el último borrador pendiente). `200` con el mensaje enviado; `409 window_closed`
  fuera de las 24 h del último inbound; `409 already_sent`; `404 no_draft`; `502
  meta_send_failed:<code>` si Meta falla (el borrador sigue pendiente). Sin edición del texto
  en L4.

## Ejemplo de llamada (PowerShell)

```powershell
$body = @{ wa_message_id = "wamid.test1"; from = "5491155551234"; timestamp = "1790000000"; type = "text"; text = "Hola Bernardo, soy Corralón Norte. Mandame el pedido Obra Palermo - mampostería." } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$env:LAB_BASE_URL/whatsapp/inbound" -Headers @{ "X-Bernardo-Lab-Secret" = $env:LAB_SHARED_SECRET } -ContentType "application/json" -Body $body
```
