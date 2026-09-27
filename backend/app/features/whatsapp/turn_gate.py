"""Un turno del agente a la vez por conversación, y ráfagas fundidas en un solo turno (L5b).

Registro en memoria por ``conversation_id``: un ``threading.Lock`` y un contador de jobs
encolados. Vale para un solo proceso uvicorn (Render starter).

Uso desde ``inbound``:

* ``decide`` encola: ``enqueue(conversation_id)`` cuando la acción es "agent".
* ``run_agent_job`` toma el turno: ``acquire(conversation_id)`` bloquea hasta que sea su
  turno y devuelve cuántos jobs más nuevos siguen esperando. Si hay alguno, ese job solo
  persiste su inbound; el último de la ráfaga corre el agente viendo todos los mensajes.
* ``release(conversation_id)`` siempre, en ``finally``. Si ya no hay pendientes ni
  esperando, la entrada se borra.
"""

import threading
from dataclasses import dataclass
from dataclasses import field


@dataclass
class _Entry:
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: Jobs encolados que todavía no tomaron el turno.
    pending: int = 0
    #: Jobs bloqueados en acquire() en este momento.
    waiters: int = 0


class TurnGate:
    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._entries: dict[int, _Entry] = {}

    def _entry(self, conversation_id: int) -> _Entry:
        return self._entries.setdefault(conversation_id, _Entry())

    def enqueue(self, conversation_id: int) -> int:
        """Un job nuevo para la conversación. Devuelve cuántos hay encolados."""

        with self._mutex:
            entry = self._entry(conversation_id)
            entry.pending += 1

            return entry.pending

    def acquire(self, conversation_id: int) -> int:
        """Bloquea hasta tener el turno. Devuelve cuántos jobs más nuevos siguen encolados."""

        with self._mutex:
            entry = self._entry(conversation_id)
            entry.waiters += 1

        entry.lock.acquire()

        with self._mutex:
            entry.waiters -= 1
            entry.pending = max(0, entry.pending - 1)

            return entry.pending

    def release(self, conversation_id: int) -> None:
        with self._mutex:
            entry = self._entries.get(conversation_id)

        if entry is None:
            return

        if entry.lock.locked():
            entry.lock.release()

        with self._mutex:
            if entry.pending == 0 and entry.waiters == 0 and not entry.lock.locked():
                self._entries.pop(conversation_id, None)

    def pending(self, conversation_id: int) -> int:
        with self._mutex:
            entry = self._entries.get(conversation_id)

            return entry.pending if entry else 0

    def reset(self) -> None:
        """Solo para tests."""

        with self._mutex:
            self._entries.clear()


gate = TurnGate()
