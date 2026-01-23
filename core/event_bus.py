from PySide6.QtCore import QObject, Signal

class EventBus(QObject):
    """
    Central event hub for module communication.
    Decouples modules so they don't need to import each other.
    """
    # Define standard signals/events here
    # Using a generic 'event_emitted' signal for flexibility: (topic, data)
    event_emitted = Signal(str, object)

    def __init__(self):
        super().__init__()
        self._subscribers = {}

    def subscribe(self, topic, callback):
        """
        Subscribe a callback function to a specific topic.
        """
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        self._subscribers[topic].append(callback)

    def publish(self, topic, data=None):
        """
        Publish an event to a specific topic.
        """
        # Emit the generic signal for global listeners (like loggers)
        self.event_emitted.emit(topic, data)

        # Notify direct subscribers
        if topic in self._subscribers:
            for callback in self._subscribers[topic]:
                try:
                    callback(data)
                except Exception as e:
                    print(f"Error in subscriber for {topic}: {e}")
