"""Prompts as versioned artifacts (L2).

A prompt is a deployed artifact with a version, a content hash and an owner,
not a string literal three call-frames deep. Three consequences follow, and all
three are requirements:

* every response records the exact prompt version that produced it, so a result
  in the run store can be traced back to the text that generated it;
* two prompt sets can be run over the same evaluation set and compared, because
  selecting one is configuration (`PROMPT_SET`) rather than a code change;
* a prompt edit changes the content hash, which invalidates the whole-run cache
  without anyone having to remember to flush it.

Layout:

    prompts/
      registry.yaml          -- sets, versions, owners, what changed and why
      v1/<step>.md           -- the baseline set
      v2/<step>.md           -- the current set
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

import yaml

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger("prompts")


@dataclass(frozen=True)
class Prompt:
    step: str
    set_name: str
    version: str
    content_sha256: str
    system: str
    user_template: str
    notes: str = ""

    @property
    def version_id(self) -> str:
        """What gets written into the result and into the trace."""
        return f"{self.step}@{self.set_name}+{self.content_sha256[:8]}"

    def render(self, **variables: Any) -> list[dict[str, str]]:
        """Safe substitution: a missing variable leaves the placeholder visible
        in the prompt rather than raising, because a half-rendered prompt that
        produces a visibly wrong answer is easier to debug than a run that dies
        at the template layer."""
        user = Template(self.user_template).safe_substitute(**variables)
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": user},
        ]


class PromptRegistry:
    def __init__(self, directory: str | None = None) -> None:
        self.dir = Path(directory or get_settings().prompt_dir)
        self._manifest: dict[str, Any] = {}
        self._cache: dict[tuple[str, str], Prompt] = {}
        self._lock = threading.RLock()
        self._load_manifest()

    def _load_manifest(self) -> None:
        manifest_path = self.dir / "registry.yaml"
        if not manifest_path.exists():
            raise FileNotFoundError(f"prompt registry not found: {manifest_path}")
        self._manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}

    @property
    def sets(self) -> list[str]:
        return list((self._manifest.get("sets") or {}).keys())

    def describe(self) -> dict[str, Any]:
        return self._manifest

    def get(self, step: str, set_name: str | None = None) -> Prompt:
        set_name = set_name or get_settings().prompt_set
        key = (set_name, step)
        with self._lock:
            if key in self._cache:
                return self._cache[key]

            sets = self._manifest.get("sets") or {}
            if set_name not in sets:
                raise KeyError(
                    f"unknown prompt set {set_name!r}; registry has {sorted(sets)}"
                )
            spec = (sets[set_name].get("prompts") or {}).get(step)
            if spec is None:
                raise KeyError(f"prompt set {set_name!r} has no step {step!r}")

            path = self.dir / set_name / f"{step}.md"
            if not path.exists():
                raise FileNotFoundError(f"prompt file missing: {path}")
            raw = path.read_text(encoding="utf-8")
            system, user = _split(raw, path)

            prompt = Prompt(
                step=step,
                set_name=set_name,
                version=str(sets[set_name].get("version", set_name)),
                content_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                system=system,
                user_template=user,
                notes=spec.get("notes", "") if isinstance(spec, dict) else "",
            )
            self._cache[key] = prompt
            return prompt

    def fingerprint(self, set_name: str | None = None) -> str:
        """One hash over an entire set. Used as a cache key component (D7)."""
        set_name = set_name or get_settings().prompt_set
        steps = sorted(((self._manifest.get("sets") or {}).get(set_name, {}).get("prompts") or {}))
        digest = hashlib.sha256()
        for step in steps:
            digest.update(self.get(step, set_name).content_sha256.encode())
        return digest.hexdigest()[:16]


_SYSTEM_MARKER = "## SYSTEM"
_USER_MARKER = "## USER"


def _split(raw: str, path: Path) -> tuple[str, str]:
    if _SYSTEM_MARKER not in raw or _USER_MARKER not in raw:
        raise ValueError(
            f"{path} must contain a '{_SYSTEM_MARKER}' and a '{_USER_MARKER}' section"
        )
    _, remainder = raw.split(_SYSTEM_MARKER, 1)
    system, user = remainder.split(_USER_MARKER, 1)
    return system.strip(), user.strip()


_registry: PromptRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> PromptRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = PromptRegistry()
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None
