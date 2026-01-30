from __future__ import annotations

from ...core.engine import Engine
from ...core.runner_controller import ProcessRunnerController


class RunnerManager:
    def __init__(self, engine: Engine):
        self._controller = ProcessRunnerController(engine)

    @property
    def running(self) -> bool:
        return self._controller.running

    def start(self, once: bool = False) -> None:
        self._controller.start(once=once)

    def resume(self, once: bool = False) -> None:
        self._controller.resume(once=once)

    def stop(self) -> None:
        self._controller.stop()

    def kill(self) -> None:
        self._controller.kill()
