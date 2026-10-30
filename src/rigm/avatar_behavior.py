"""Event planner shared by RigM's 2D and optional humanoid avatar runtimes."""

from __future__ import annotations

from typing import Any

from .behavior import BEHAVIOR_STATES, motion_event_from_agent
from .project import ProjectError


_STATE_ACTION = {
    "idle": ("idle", "stand"),
    "listening": ("listen", "stand"),
    "thinking": ("think", "stand"),
    "speaking": ("talk", "stand"),
    "waiting": ("wait", "stand"),
    "restless": ("pace", "pace"),
    "error": ("recover", "stand"),
}
_ALLOWED_INTENTS = {"greet", "explain", "agree", "ask", "shrug", "point", "celebrate", "apologize"}


class AvatarBehaviorPlanner:
    """Convert public agent/TTS signals into a small, renderer-neutral motion plan.

    The planner decides which authored action and locomotion layer to use. The
    viewer is responsible for blending its clips, procedural face controls, and
    secondary physics at render frequency.
    """

    def __init__(self) -> None:
        self.state = "idle"
        self.intensity = 0.3
        self.valence = 0.0
        self.gaze = [0.0, 0.0]
        self.intent: str | None = None
        self.viseme = {"shape": "silence", "duration": 0.12}
        self.speech_energy = 0.0
        self.speech_emphasis = 0.0
        self.elapsed = 0.0
        self.transition_seconds = 0.45

    def dispatch(self, event: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(event, dict):
            raise ProjectError("El evento de avatar debe ser un objeto JSON.")
        kind = str(event.get("type", "")).casefold()
        normalized = motion_event_from_agent(event) if kind.startswith(("agent.", "tts.", "speech.")) else event
        kind = str(normalized.get("type", "")).casefold()
        if kind == "state":
            state = str(normalized.get("state", "")).casefold()
            if state not in BEHAVIOR_STATES:
                raise ProjectError(f"Estado de avatar desconocido: {state}.")
            if state != self.state:
                self.elapsed = 0.0
            self.state = state
            self.intensity = max(0.0, min(1.0, float(normalized.get("intensity", self.intensity))))
            self.valence = max(-1.0, min(1.0, float(normalized.get("valence", self.valence))))
        elif kind == "gaze":
            self.gaze = [max(-1.0, min(1.0, float(normalized.get("x", 0.0)))), max(-1.0, min(1.0, float(normalized.get("y", 0.0))))]
        elif kind == "viseme":
            if self.state != "speaking":
                self.elapsed = 0.0
            self.state = "speaking"
            self.viseme = {"shape": str(normalized.get("shape", "open")), "duration": max(0.04, float(normalized.get("duration", 0.12)))}
        elif kind == "speech":
            if self.state != "speaking":
                self.elapsed = 0.0
            self.state = "speaking"
            self.speech_energy = max(0.0, min(1.0, float(normalized.get("mouth_open", normalized.get("level", 0.0)))))
            self.speech_emphasis = max(0.0, min(1.0, float(normalized.get("emphasis", 0.0))))
        elif kind == "gesture":
            gesture = str(normalized.get("name", "")).casefold()
            if gesture not in _ALLOWED_INTENTS:
                raise ProjectError(f"Intención gestual no permitida: {gesture}.")
            self.intent = gesture
        elif kind == "gesture_end":
            self.intent = None
        elif kind == "parameter":
            if normalized.get("name") == "intensity":
                self.intensity = max(0.0, min(1.0, float(normalized.get("value", 0.0))))
            elif normalized.get("name") == "valence":
                self.valence = max(-1.0, min(1.0, float(normalized.get("value", 0.0))))
            else:
                raise ProjectError("Parámetro de avatar no reconocido.")
        else:
            raise ProjectError(f"Evento de avatar no reconocido: {kind}.")
        return self.plan()

    def update(self, dt: float) -> dict[str, Any]:
        self.elapsed += max(0.0, min(float(dt), 0.25))
        return self.plan()

    def plan(self) -> dict[str, Any]:
        action, locomotion = _STATE_ACTION[self.state]
        expression = "neutral"
        if self.valence > 0.45:
            expression = "happy"
        elif self.valence < -0.45:
            expression = "sad"
        if self.state == "error":
            expression = "sad"
        return {
            "state": self.state,
            "action": action,
            "locomotion": locomotion,
            "intent": self.intent,
            "intensity": self.intensity,
            "valence": self.valence,
            "expression": expression,
            "gaze": list(self.gaze),
            "viseme": dict(self.viseme),
            "speech": {"energy": self.speech_energy, "emphasis": self.speech_emphasis},
            "transition_seconds": self.transition_seconds,
            "state_elapsed": self.elapsed,
        }
