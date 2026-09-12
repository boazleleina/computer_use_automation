"""The composition root.

The one module permitted to import from adapters. It reads configuration and
constructs the port implementations, choosing PlaywrightSurface or
ScriptedSurface, ClaudeModel or RecordedModel, and hands them to a use case.

Everything else in the system is written against ports and therefore cannot
know which implementation it received. That is what lets the same capability
run against a live browser and a JSON fixture without changing a line.

Kept as a single module on purpose. A dependency injection container would hide
the wiring, and the wiring is the first thing to check when a run behaves
differently against two surfaces.

This is the only layer that uses pydantic. config.yaml and .env are untrusted
text until something validates them, and that validation belongs here rather
than in the domain: a settings error is a wiring problem, caught once at
startup, not a business rule. The domain receives `Policy`, `RedactionRules`
and the rest as plain frozen values, and could not tell they came from a file.

What is deliberately not here: any decision. This module knows which classes
exist and what the file said. It does not know what a capability means, when a
run should stop, or whether an action is allowed — those live in the domain,
and a composition root that started answering them would be a second place to
look for the rules.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cua.adapters.clocks import RealClock
from cua.adapters.errors import ConfigurationError
from cua.adapters.fs_artifact_store import FilesystemArtifactStore
from cua.adapters.fs_evidence_sink import FilesystemEvidenceSink
from cua.app.discover import DiscoveryLimits
from cua.domain.actions import ActionType
from cua.domain.policy import (
    DeniedControl,
    Policy,
    RedactionRules,
    Rendering,
    Sensitivity,
)
from cua.ports.artifact_store import ArtifactStore
from cua.ports.clock import Clock
from cua.ports.evidence import EvidenceSink
from cua.ports.surface import Surface

DEFAULT_CONFIG = Path("config.yaml")


class Strict(BaseModel):
    """A settings block that refuses keys nothing reads.

    A config key nobody consumes is a lie that survives review: it reads as a
    knob, and turning it does nothing. Five of them accumulated before this
    was added — capture_dom, step_ms, never_capture among them — each looking
    like a control over behaviour it had no effect on.
    """

    model_config = ConfigDict(extra="forbid")


class SurfaceSettings(Strict):
    """Which surface, and where it points."""

    kind: str = "playwright"
    base_url: str = "http://127.0.0.1:5055"
    headless: bool = False
    viewport: dict[str, int] = Field(default_factory=lambda: {"width": 1280, "height": 800})


class ModelSettings(Strict):
    provider: str = "anthropic"
    name: str
    max_tokens: int = 4096


class DiscoverySettings(Strict):
    max_steps: int = 40
    run_ms: int = 900_000
    stop_on_repeated_state: bool = True


class PolicySettings(Strict):
    """The guardrails, as text, before anything has checked them.

    Actions are validated against ActionType here rather than downstream: a
    config naming an action nobody implements is a wiring mistake, and the
    place to find out is startup rather than the middle of a run.
    """

    allowed_origins: list[str] = Field(default_factory=list)
    allowed_routes: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    denied_controls: list[dict[str, str]] = Field(default_factory=list)


class RedactionSettings(Strict):
    secret: str = "drop"
    personal: str = "mask"
    internal: str = "record"
    unclassified: str = "drop"


class EvidenceSettings(Strict):
    dir: str = "evidence"
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)


class ArtifactSettings(Strict):
    """Where stored capabilities live. Separate from evidence: an artifact is
    the product, evidence is proof of a run."""

    dir: str = "capabilities"


class TimeoutSettings(Strict):
    action_ms: int = 5000


class Settings(Strict):
    """config.yaml, validated.

    Every section has defaults, so a partial file is a usable file. The one
    thing without one is the model's name: a run that has to say afterwards
    which model made its decisions cannot fall back on a guess.
    """

    surface: SurfaceSettings = Field(default_factory=SurfaceSettings)
    model: ModelSettings
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    evidence: EvidenceSettings = Field(default_factory=EvidenceSettings)
    artifacts: ArtifactSettings = Field(default_factory=ArtifactSettings)
    timeout: TimeoutSettings = Field(default_factory=TimeoutSettings)


def load_settings(path: Path = DEFAULT_CONFIG) -> Settings:
    """Read and validate the configuration file.

    A bad config fails here with the field named, rather than three layers
    down as an AttributeError on something that was never there.
    """
    try:
        document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigurationError(f"could not read {path}: {error}") from error
    except yaml.YAMLError as error:
        raise ConfigurationError(f"{path} is not valid YAML: {error}") from error

    if not isinstance(document, dict):
        raise ConfigurationError(f"{path} does not contain a configuration document")

    try:
        return Settings.model_validate(document)
    except ValidationError as error:
        raise ConfigurationError(f"{path} is not a usable configuration: {error}") from error


def policy_from(settings: Settings) -> Policy:
    """The guardrails as the domain sees them: a frozen value, no file in it."""
    actions = set()
    for name in settings.policy.allowed_actions:
        try:
            actions.add(ActionType(name))
        except ValueError as error:
            raise ConfigurationError(
                f"policy.allowed_actions names {name!r}, which is not an action this "
                f"system implements; the set is {[a.value for a in ActionType]}"
            ) from error

    return Policy(
        allowed_origins=tuple(settings.policy.allowed_origins),
        allowed_routes=tuple(settings.policy.allowed_routes),
        allowed_actions=frozenset(actions),
        denied_controls=tuple(
            DeniedControl(role=entry.get("role", ""), name=entry.get("name", ""))
            for entry in settings.policy.denied_controls
        ),
    )


def redaction_from(settings: Settings) -> RedactionRules:
    rules = settings.evidence.redaction
    try:
        return RedactionRules(
            secret=Rendering(rules.secret),
            personal=Rendering(rules.personal),
            internal=Rendering(rules.internal),
            unclassified=Rendering(rules.unclassified),
        )
    except ValueError as error:
        raise ConfigurationError(
            f"evidence.redaction names a rendering that does not exist: {error}"
        ) from error


def limits_from(settings: Settings) -> DiscoveryLimits:
    return DiscoveryLimits(
        max_steps=settings.discovery.max_steps,
        run_ms=settings.discovery.run_ms,
        stop_on_repeated_state=settings.discovery.stop_on_repeated_state,
    )


def evidence_sink(
    settings: Settings,
    declared: dict[str, Sensitivity] | None = None,
    known_values: dict[str, Sensitivity] | None = None,
    stream_name: str = "events.jsonl",
) -> EvidenceSink:
    """The one implementation that writes anything durable.

    `declared` and `known_values` are the caller's, because only the caller
    knows what this particular run is handling — which member, which operator.
    A sink built without them still works and drops almost everything, because
    unclassified fails closed.
    """
    return FilesystemEvidenceSink(
        root=Path(settings.evidence.dir),
        rules=redaction_from(settings),
        declared=declared or {},
        known_values=known_values or {},
        stream_name=stream_name,
    )


@contextmanager
def surface_session(settings: Settings) -> Iterator[Surface]:
    """The configured surface, open for the length of a run.

    A context manager because a browser is a resource and the run owns it for
    its whole length — which is also what makes a handover possible, since the
    person is handed the session the run was using rather than a fresh one.

    The import is inside the function on purpose. Constructing a Playwright
    surface should not be the cost of running `cua --help`, and a deployment
    that only ever replays a scripted fixture has no reason to need a browser
    installed.
    """
    if settings.surface.kind == "playwright":
        from cua.adapters.playwright_surface import browser_session

        with browser_session(
            settings.surface.base_url,
            headless=settings.surface.headless,
            viewport=settings.surface.viewport,
            action_timeout_ms=settings.timeout.action_ms,
        ) as surface:
            yield surface
        return

    raise ConfigurationError(
        f"surface.kind is {settings.surface.kind!r}; this build constructs 'playwright'. "
        "The scripted surface is a test fixture and is wired by the test that wants it."
    )


def artifact_store(settings: Settings) -> ArtifactStore:
    """Where capabilities are kept between being compiled and being run.

    The one store, so that "which version was approved" has one answer.
    Versions are never overwritten there; publishing over an approved artifact
    is refused rather than resolved in favour of whoever wrote last.
    """
    return FilesystemArtifactStore(root=Path(settings.artifacts.dir))


def clock() -> Clock:
    """Real time. A fake one is a test's business, not a config option:
    nothing good comes of a production run being able to stop the clock."""
    return RealClock()
