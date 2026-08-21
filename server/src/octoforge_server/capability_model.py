"""One startup-reported capability and shared rendering constants."""

from dataclasses import dataclass

REPORT_HEADER = "capabilities of this installation:"
ON = "on"
OFF = "off"
CRITICAL = frozenset({"embeddings", "operator credential"})


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    enabled: bool
    detail: str

    def line(self) -> str:
        return f"  {self.name:<20} {ON if self.enabled else OFF:<3}  {self.detail}"
