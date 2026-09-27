# COMPARATIVO POR PEDIDO (lab L5d)

**Fecha**: 2026-09-27
**Estado**: vigente para L5d
**Base de diseño**: `dashboard/docs/design/sesion-comparador-presupuestos-modelo-fiscal-y-ponderacion.md` (secciones 2, 4, 5 y 9)
**Código**: `backend/app/features/batch_comparison/` (loader, fiscal, strategies, porque, excel, service, router)

## Qué calcula

Para un pedido (`rfq_batches`) toma sus ítems y todas las `supplier_quotes` de esos ítems, de
cualquier fuente (form, manual, import, WhatsApp). Si un proveedor tiene más de una cotización
para el mismo ítem, vale la más reciente por `submitted_at`. Con eso arma:

1. una **matriz** ítem × proveedor con precio cotizado, neto, costo real, desembolso, flete,
   plazo, pago, validez y marcas;
2. dos **estrategias de compra** con su "porqué" en castellano;
3. un **Excel** con tres hojas (Matriz, Estrategias, Supuestos).

No llama a ningún modelo. Toda la aritmética es `Decimal`; se redondea a dos decimales solo al
presentar.

## Fórmulas (por cotización y por ítem)

| Caso | neto_unit |
| --- | --- |
| `iva_included = true` | precio / (1 + alícuota/100) |
| `iva_included = false` | precio |
| `iva_included = null` | precio (se asume **sin** IVA, el caso más caro) + marca `IVA sin confirmar` |

- `desembolso_unit = neto_unit × (1 + alícuota/100)` — la plata que sale.
- `costo_real_unit (β=1) = neto_unit` — el IVA vuelve como crédito fiscal.
- Totales = unitario × `rfqs.quantity`. Si `shipping_cost` no es nulo se suma al neto total del
  ítem y se anota. Si `freight_included = false` y no hay costo → marca `flete a cotizar` (el
  costo queda sin flete).
- Las estrategias ordenan por **costo real total**; empate por precio → menor plazo → nombre.

## Régimen del proveedor (L5f)

Lo que el proveedor dijo por WhatsApp queda en `whatsapp_conversations` (por proveedor × pedido,
herramienta `record_terms`) y el comparativo lo lee por proveedor (`regime.py`). Fórmula del
diseño: `costo_real = desembolso − base_documentada × alícuota × β`, con β = 1.

| Régimen | Desembolso | Costo real | Marca |
| --- | --- | --- | --- |
| facturado | neto × (1 + a) | neto | — |
| null (sin conversación o sin dato) | igual que facturado | igual que facturado | `régimen sin confirmar, se asume facturado` |
| efectivo | precio × cantidad, tal cual | = desembolso | `en efectivo, sin factura` |
| parcial con `documented_pct` = p | precio × cantidad | desembolso − (desembolso × p/100) / (1 + a) × a | `factura el p%` |
| parcial sin % | precio × cantidad | = desembolso | `factura una parte, % sin dato: costo real sin crédito de IVA` |

Con efectivo o parcial `iva_included` se ignora y no genera `IVA sin confirmar`. Regla de oro
fiscal: regímenes distintos no se comparan sin más; una estrategia que los mezcla lleva la marca
`mezcla facturado y efectivo: comparar con cuidado`.

## Flete (L5f)

El flete se suma **una vez por proveedor usado** en cada estrategia (`freight.py`), nunca por ítem
ni prorrateado. El `shipping_cost` por ítem de `supplier_quotes` ya no se suma.

- Sin condiciones o flete incluido → 0. Sin dato → 0 con `flete a cotizar` o `flete sin confirmar`.
- Umbral `freight_free_over`: si la suma de `unit_price × quantity` (tal como cotizó) de lo que la
  estrategia le asigna al proveedor es ≥ umbral → 0 con `flete sin cargo: pedido de $ S supera $ X`;
  si no, `freight_cost` con `flete $ F: pedido de $ S no llega a $ X`; sin costo → 0 y
  `flete a cotizar por debajo de $ X`.
- `freight_basis = viaje` → se suma una vez con `flete $ F por viaje: 1 viaje supuesto`.
- El flete no lleva IVA en el costo real. En el desembolso va × (1 + 21/100) si el régimen es
  facturado y tal cual si no.

## Supuestos

- **β = 1** para el costo real: el comprador computa el IVA contra el débito fiscal de la obra.
  Si no lo recupera (β = 0), el número que le importa es el desembolso. Los dos van lado a lado.
- **Alícuota por ítem**, default 21 %. Se pisa por ítem con el query `alicuotas`.
- **Moneda ARS**. Una cotización en otra moneda aparece en la matriz con la marca
  `moneda <X>, no comparada` y no entra en las estrategias.
- Sin precio → la cotización no participa para ese ítem (marca `sin precio`).

## Las dos estrategias

- **Menor costo total** (clave `menor_costo_total`, antes "menor costo por ítem"): el subconjunto
  de proveedores que cubre los ítems cubribles con menor total, contando el flete de cada
  proveedor usado una vez y cada ítem al más barato dentro del subconjunto. Fuerza bruta hasta 12
  proveedores; con más, el más barato por ítem más flete, con `resultado aproximado`. Un ítem sin
  cotización comparable queda `sin cotización`.
- **Menos proveedores**: el mínimo k de proveedores que cubre todos los ítems cubribles y, entre
  los subconjuntos de tamaño k, el de menor total con flete. Fuerza bruta hasta 12 proveedores;
  con más, greedy y marca `resultado aproximado`.

Cada estrategia informa asignación ítem → proveedor, flete por proveedor, total costo real,
total desembolso, cantidad de proveedores, plazo máximo, marcas de las cotizaciones elegidas y el
porqué, por ejemplo: *"Comprando a 1 proveedor (Materiales del Sur) en lugar de 2 pagás
$ 255.578,51 más (12,4%). A cambio coordinás una entrega menos. Consolidar en Materiales del Sur
ahorra $ 40.000,00 de flete neto."* Al final va la diferencia entre estrategias en pesos y en
porcentaje.

## Marcas posibles

`IVA sin confirmar` · `flete a cotizar` · `flete sin confirmar` · `flete sin cargo: ...` ·
`flete $ F: pedido de $ S no llega a $ X` · `flete a cotizar por debajo de $ X` ·
`flete $ F por viaje: 1 viaje supuesto` · `régimen sin confirmar, se asume facturado` ·
`en efectivo, sin factura` · `factura el p%` · `factura una parte, % sin dato: ...` ·
`mezcla facturado y efectivo: comparar con cuidado` ·
`falta plazo` · `falta forma de pago` · `falta validez` · `moneda <X>, no comparada` ·
`sin precio` · `sin cotización` (ítem sin cotización comparable) · `resultado aproximado` ·
`precio leído de un adjunto` (L5e: la cotización tiene `from_attachment` en `risk_flags`
porque el precio salió de un PDF o una foto transcriptos; el JSON lo expone como
`from_attachment: true`, la Matriz lo muestra como marca, Supuestos lo aclara y el porqué
lo lista entre las salvedades que cambian el costo: "revisar contra el archivo").

## Endpoints

Ambos con header `X-Bernardo-Lab-Admin: <LAB_ADMIN_TOKEN>` (mismo token que la aprobación de
borradores). Sin token o distinto → `401`. Batch inexistente → `404`.

- `GET /rfq-batches/{id}/comparison` → JSON con `batch`, `suppliers` (con estado de la
  conversación de WhatsApp si existe y, desde L5f, `billing_regime`, `documented_pct` y
  `freight` {included, cost, basis, free_over}), `items` (matriz; cada cotización trae
  `billing_regime`, `documented_pct` y `quoted_total`), `strategies` (con `total_freight` y
  `freight_by_supplier`) y `difference`. Los montos son strings con dos decimales
  (`"41200.00"`), nunca float.
- `GET /rfq-batches/{id}/comparison.xlsx` → Excel con `Content-Disposition:
  attachment; filename="comparativo-<pedido>-<fecha>.xlsx"`.
- Query opcional `alicuotas="<rfq_id>:<pct>,<rfq_id>:<pct>"` (decimal con punto:
  `12:10.5,13:21`). Mal formada, fuera de 0–100 o con un ítem ajeno al pedido → `422`.

## Bajar el Excel (PowerShell)

```powershell
$headers = @{ "X-Bernardo-Lab-Admin" = $env:LAB_ADMIN_TOKEN }
Invoke-WebRequest -Uri "$env:LAB_BASE_URL/rfq-batches/12/comparison.xlsx" -Headers $headers -OutFile "comparativo-12.xlsx"

# Con alícuota 10,5 en el ítem 40 y guardando los bytes a mano:
$r = Invoke-WebRequest -Uri "$env:LAB_BASE_URL/rfq-batches/12/comparison.xlsx?alicuotas=40:10.5" -Headers $headers
[System.IO.File]::WriteAllBytes("$PWD\comparativo-12.xlsx", $r.Content)
```

## Demo local (solo SQLite)

```powershell
cd backend
$env:DATABASE_URL = "sqlite:///var/lab_demo_comparison.db"
$env:DATABASE_URL_DIRECT = $env:DATABASE_URL
uv run python -m scripts.lab_demo_comparison
```

Si `DATABASE_URL` no empieza con `sqlite`, el script frena con mensaje y código 1. Nunca
escribe en Supabase.
