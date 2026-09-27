"""Simulador de proveedores para el agente de WhatsApp (laboratorio Bernardo, L3b).

Siete personas, cada una con una situación distinta, conversan con Bernardo sobre el
Excel de ejemplo de L2. Un juez determinístico decide si el agente inventó datos,
comprometió una compra, derivó lo que no sabía y cerró en el estado esperado. Cada corrida
deja SQLite propio, transcripts y reporte en ``backend/.sim/runs/<stamp>/``.

Nada acá modifica el agente: si algo falla, se reporta y Diego decide.
"""
