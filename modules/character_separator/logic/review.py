from dataclasses import dataclass, field
from pathlib import Path

from .export import ExportError, ExportResult, FileSignature, export
from .recognition import Sample, classify


@dataclass(frozen=True)
class Decision:
    name: str | None
    sample: Sample
    reference: bool = False


@dataclass(frozen=True)
class FullRun:
    output: ExportResult
    pending: int
    unreadable: int
    error: ExportError | None = None
    signatures: dict[Path, FileSignature] = field(default_factory=dict)


@dataclass
class ReviewSession:
    """Decisions for one source folder while its review is open."""

    decisions: dict[Path, Decision] = field(default_factory=dict)
    cache: dict[Path, Sample] = field(default_factory=dict)
    seen: set[Path] = field(default_factory=set)
    exported: set[Path] = field(default_factory=set)
    destinations: dict[str, set[Path]] = field(default_factory=dict)
    receipts: dict[Path, tuple[str, Path]] = field(default_factory=dict)
    rejections: dict[Path, dict[str, FileSignature]] = field(default_factory=dict)
    receipt_signatures: dict[Path, FileSignature] = field(default_factory=dict)

    def references(self):
        choices = {}
        for decision in self.decisions.values():
            if decision.name:
                choices.setdefault(decision.name, []).append(decision)
        return {
            name: tuple(decision.sample for decision in decisions if decision.reference)
            or (decisions[0].sample,)
            for name, decisions in choices.items()
        }

    def next_batch_exclusions(self):
        return self.seen.union(self.decisions, self.exported)

    def assigned_images(self) -> dict[str, dict[Path, FileSignature]]:
        """Include verified automatic exports alongside manually identified images."""
        assignments = {}
        for path, decision in self.decisions.items():
            if decision.name is not None:
                assignments.setdefault(decision.name, {})[path] = decision.sample.signature
        for path, (name, _) in self.receipts.items():
            if path not in self.decisions and path in self.receipt_signatures:
                assignments.setdefault(name, {})[path] = self.receipt_signatures[path]
        return assignments

    def _forget_receipts(self, paths):
        removed = set()
        for path in paths:
            receipt = self.receipts.pop(path, None)
            if receipt is not None:
                removed.add(receipt)
            self.receipt_signatures.pop(path, None)
            self.exported.discard(path)
        if not removed:
            return
        for name, destination in removed.difference(self.receipts.values()):
            self.destinations[name].discard(destination)
            if not self.destinations[name]:
                del self.destinations[name]

    def apply_decisions(self, decisions: dict[Path, Decision]):
        self._forget_receipts(
            path for path, decision in decisions.items()
            if path in self.receipts and self.receipts[path][0] != decision.name
        )
        self.decisions.update(decisions)

    def full_run(self, mother, source, cancelled, progress):
        scanned = classify(
            source, self.references(), self.exported.union(self.decisions),
            cancelled, progress, cache=self.cache, rejected=self.rejections,
        )
        assignments = {}
        signatures = {}
        for path, decision in self.decisions.items():
            if decision.name and path not in self.exported:
                assignments.setdefault(decision.name, []).append(path)
                signatures[path] = decision.sample.signature
        for path, name in scanned.matches.items():
            assignments.setdefault(name, []).append(path)
            signatures[path] = scanned.signatures[path]
        failed = len(scanned.failed)
        pending = scanned.processed - len(scanned.matches) - failed
        try:
            output = export(
                mother, source, assignments, cancelled, progress, expected=signatures,
            )
        except ExportError as error:
            return FullRun(error.result, pending, failed, error, signatures)
        return FullRun(output, pending, failed, signatures=signatures)

    def apply(self, run):
        self.exported.update(run.output.completed)
        self.receipts.update(zip(run.output.completed, run.output.destinations))
        self.receipt_signatures.update(
            (path, run.signatures[path]) for path in run.output.completed if path in run.signatures
        )
        for name, path in run.output.destinations:
            self.destinations.setdefault(name, set()).add(path)
        if not run.output.cancelled and run.error is None:
            self.seen.clear()
        elif run.error and run.error.code == "source_changed":
            path = run.error.path
            self.decisions.pop(path, None)
            self.seen.discard(path)
            self.cache.pop(path, None)
            self._forget_receipts((path,))

    def delete_character(self, name):
        assigned = {path for path, decision in self.decisions.items() if decision.name == name}
        organized = {path for path, (character, _) in self.receipts.items() if character == name}
        for path in assigned:
            del self.decisions[path]
        self._forget_receipts(organized)
        for path in tuple(self.rejections):
            rejected = self.rejections[path]
            rejected.pop(name, None)
            if not rejected:
                del self.rejections[path]
        self.seen.difference_update(assigned | organized)
