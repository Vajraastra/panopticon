from PySide6.QtCore import QObject, Signal

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
                    print(f"Error in subscriber for {topic}: {e}")
