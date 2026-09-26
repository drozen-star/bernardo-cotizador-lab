"""Esquemas de las herramientas del agente (spec 4.2 y 8).

Ninguna tiene efectos hacia afuera: todas escriben en la base. Los esquemas son estrictos
(``additionalProperties: false``, todos los campos requeridos) para que el modelo diga
``null`` de forma explícita en lo que el proveedor no dijo, en vez de omitirlo o inventarlo.
"""

RECORD_QUOTE = "record_quote"
ASK_BUYER = "ask_buyer"
SET_STATUS = "set_status"

#: Estados que el modelo puede pedir con set_status. "expired" lo pone el sistema.
STATUS_VALUES = ("complete", "supplier_declined", "needs_human")

CURRENCIES = ("ARS", "USD")

TOOLS: list[dict] = [
    {
        "name": RECORD_QUOTE,
        "description": (
            "Registra el precio y las condiciones que el proveedor informó para UN ítem del "
            "pedido. Llamala una vez por ítem cada vez que el proveedor pase un precio o una "
            "condición, aunque sea parcial, y también cuando corrija un valor ya registrado. "
            "Copiá los valores tal como los dio: no redondees, no conviertas monedas, no "
            "completes lo que no dijo (va en null). rfq_id es el de la ficha del pedido. "
            "evidence es el fragmento literal del mensaje del proveedor que respalda el "
            "precio; si el fragmento no aparece en lo que escribió, el registro se rechaza."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "rfq_id": {"type": "integer", "description": "rfq_id del ítem, según la ficha."},
                "unit_price": {
                    "type": ["number", "null"],
                    "description": "Precio unitario tal como lo dijo el proveedor.",
                },
                "currency": {
                    "type": ["string", "null"],
                    "enum": [*CURRENCIES, None],
                    "description": "ARS o USD si lo dijo o es inequívoco; si no, null.",
                },
                "iva_included": {
                    "type": ["boolean", "null"],
                    "description": "true si el precio incluye IVA, false si no, null si no lo aclaró.",
                },
                "freight_included": {
                    "type": ["boolean", "null"],
                    "description": "true si el flete a obra está incluido, false si no, null si no lo aclaró.",
                },
                "lead_time_days": {
                    "type": ["integer", "null"],
                    "description": "Plazo de entrega en días, solo si lo dijo en días o es convertible sin dudas.",
                },
                "payment_terms": {
                    "type": ["string", "null"],
                    "description": "Forma de pago tal como la dijo, p. ej. 'contado' o '30 días'.",
                },
                "validity": {
                    "type": ["string", "null"],
                    "description": "Validez de la oferta tal como la dijo, p. ej. '48 horas' o 'hasta el 30/09'.",
                },
                "evidence": {
                    "type": "string",
                    "minLength": 3,
                    "description": "Fragmento literal del mensaje del proveedor que respalda el precio.",
                },
            },
            "required": [
                "rfq_id",
                "unit_price",
                "currency",
                "iva_included",
                "freight_included",
                "lead_time_days",
                "payment_terms",
                "validity",
                "evidence",
            ],
        },
    },
    {
        "name": ASK_BUYER,
        "description": (
            "Deriva al comprador una pregunta del proveedor que no se puede responder con la "
            "ficha del pedido: medida o marca alternativa no listada, cambio de cantidad, "
            "horarios, acceso a obra, cualquier dato que no figure. Usala en lugar de "
            "inventar. Después de llamarla, decile al proveedor que lo consultás y le confirmás."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "question": {
                    "type": "string",
                    "minLength": 3,
                    "description": "La pregunta, redactada para el comprador, con el contexto necesario.",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": SET_STATUS,
        "description": (
            "Cierra o deriva la conversación. complete: hay precio y condiciones de todo lo "
            "que el proveedor puede cotizar. supplier_declined: el proveedor dijo que no "
            "trabaja estos materiales o no va a cotizar. needs_human: pide hablar con una "
            "persona, se pone hostil o la conversación se sale del pedido."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": list(STATUS_VALUES)},
                "reason": {"type": "string", "minLength": 3},
            },
            "required": ["status", "reason"],
        },
    },
]

TOOL_NAMES = tuple(tool["name"] for tool in TOOLS)
