import logging

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class EventBus(QObject):
    """
    Bus de eventos central para la comunicación desacoplada entre módulos.
    Permite que un módulo notifique acciones (ej. "navigate") sin conocer
    quién las escuchará, evitando dependencias circulares.
    """
    # Señal genérica para observadores globales: emite (tópico, datos)
    event_emitted = Signal(str, object)

    def __init__(self):
        super().__init__()
        self._subscribers = {} # Diccionario de {tópico: [callbacks]}

    def subscribe(self, topic, callback):
        """
        Registra una función para ser llamada cuando se publique un evento en el tópico.
        :param topic: String identificador del evento.
        :param callback: Función o método a ejecutar.
        """
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        self._subscribers[topic].append(callback)

    def unsubscribe(self, topic, callback):
        """
        Cancela la suscripción de un callback a un tópico. Seguro de llamar
        aunque el callback no esté registrado (no lanza). Evita fugas de
        memoria y callbacks colgando sobre widgets destruidos.
        :param topic: String identificador del evento.
        :param callback: La misma función/método que se pasó a subscribe().
        """
        callbacks = self._subscribers.get(topic)
        if not callbacks:
            return
        try:
            callbacks.remove(callback)
        except ValueError:
            pass  # no estaba suscrito; nada que hacer
        if not callbacks:
            del self._subscribers[topic]

    def publish(self, topic, data=None):
        """
        Emite un evento a todos los suscriptores interesados.
        :param topic: El evento que se está disparando.
        :param data: Datos opcionales asociados al evento.
        """
        # Emitir señal genérica (útil para logs o depuración)
        self.event_emitted.emit(topic, data)

        # Notificar a los suscriptores directos
        if topic in self._subscribers:
            for callback in self._subscribers[topic]:
                try:
                    callback(data)
                except Exception as e:
                    log.error("Error en suscriptor de '%s': %s", topic, e)
