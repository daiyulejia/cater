"""Agent driver abstractions.

Real CLIs and APIs will plug in here later. The seed runtime uses deterministic
scripted drivers so demos and tests are stable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DriverResponse:
    text: str
    tokens: int = 0
    meta: dict[str, Any] | None = None


class Driver(Protocol):
    name: str

    def complete(self, *, role: str, prompt: str, context: dict[str, Any]) -> DriverResponse:
        ...


class ScriptedDriver:
    def __init__(self, name: str, script: list[str], tokens_per_call: int = 200):
        self.name = name
        self.script = list(script)
        self.tokens_per_call = tokens_per_call
        self.cursor = 0

    def complete(self, *, role: str, prompt: str, context: dict[str, Any]) -> DriverResponse:
        if not self.script:
            text = f'{{"stance":"assert","option_id":"A","claim":"{role} agrees","evidence":["script"],"text":"{role} agrees"}}'
        else:
            text = self.script[self.cursor % len(self.script)]
            self.cursor += 1
        return DriverResponse(text=text, tokens=self.tokens_per_call, meta={"driver": self.name})
