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

## Supuestos

- **Todo facturado**: todas las cotizaciones se asumen con factura A. Si alguna es en efectivo,
  este comparativo no la compara bien (regla de oro fiscal: regímenes distintos no se comparan).
- **β = 1** para el costo real: el comprador computa el IVA contra el débito fiscal de la obra.
  Si no lo recupera (β = 0), el número que le importa es el desembolso. Los dos van lado a lado.
- **Alícuota por ítem**, default 21 %. Se pisa por ítem con el query `alicuotas`.
- **Moneda ARS**. Una cotización en otra moneda aparece en la matriz con la marca
  `moneda <X>, no comparada` y no entra en las estrategias.
- Sin precio → la cotización no participa para ese ítem (marca `sin precio`).

## Las dos estrategias

- **Menor costo por ítem**: cada ítem al proveedor con menor costo real. Un ítem sin cotización
  comparable queda `sin cotización`.
- **Menos proveedores**: el mínimo k de proveedores que cubre todos los ítems cubribles y, entre
  los subconjuntos de tamaño k, el de menor total (cada ítem al más barato dentro del
  subconjunto). Fuerza bruta hasta 12 proveedores; con más, greedy y marca
  `resultado aproximado`.

Cada estrategia informa asignación ítem → proveedor, total costo real, total desembolso,
cantidad de proveedores, plazo máximo, marcas de las cotizaciones elegidas y el porqué, por
ejemplo: *"Comprando a 1 proveedor (Materiales del Sur) en lugar de 2 pagás $ 255.578,51 más
(12,4%). A cambio coordinás una entrega menos."* Al final va la diferencia entre estrategias en
pesos y en porcentaje.

## Marcas posibles

`IVA sin confirmar` · `flete a cotizar` · `flete sin confirmar` · `flete $ X aparte, sumado` ·
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
  conversación de WhatsApp si existe), `items` (matriz), `strategies` y `difference`. Los
  montos son strings con dos decimales (`"41200.00"`), nunca float.
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
