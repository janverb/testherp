from typing import Any, Callable

class ThreadedServer:
    def __init__(self, app: Callable[[Any, Any], Any]) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
