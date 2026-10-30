"""Event-driven, real-time behavior layer for responsive 2D characters.

The runtime is deliberately independent of the GUI and speech provider. Hosts send
small public events (state, gesture, gaze, or normalized speech cues); authored
clips and bounded procedural motion produce an ephemeral pose for each update.
"""

from __future__ import annotations

import math
import random
from typing import Any

from .project import ProjectError
from .rig import sample_clip, sample_layer_visibility


BEHAVIOR_STATES = ("idle", "listening", "thinking", "speaking", "waiting", "restless", "error")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


class BehaviorRuntime:
    """Mix an authored base clip with responsive, bounded procedural behavior."""

    def __init__(self, project: dict[str, Any], *, seed: int | None = None) -> None:
        self.project = project
        self.joints = list(project.get("joints", []))
        self.clips = list(project.get("animations", []))
        self.by_id = {str(joint["id"]): joint for joint in self.joints}
        self.semantic_joints = {
            "torso": self._joint("torso", "body"),
            "head": self._joint("cabeza", "head"),
            "neck": self._joint("cuello", "neck"),
            "left_shoulder": self._joint("hombro visual izquierdo", "left shoulder"),
            "right_shoulder": self._joint("hombro visual derecho", "right shoulder"),
            "left_elbow": self._joint("codo visual izquierdo", "left elbow"),
            "right_elbow": self._joint("codo visual derecho", "right elbow"),
            "left_hip": self._joint("cadera visual izquierda", "left hip"),
            "right_hip": self._joint("cadera visual derecha", "right hip"),
            "left_knee": self._joint("rodilla visual izquierda", "left knee"),
            "right_knee": self._joint("rodilla visual derecha", "right knee"),
        }
        self.base_clip = self._clip("Reposo", "Idle")
        self.time = 0.0
        self._dt = 0.0
        self.state = "idle"
        self.state_time = 0.0
        self.intensity = 0.35
        self.valence = 0.0
        self.gaze_input = (0.0, 0.0)
        self.speech_input: dict[str, float] | None = None
        self.speech_input_until = 0.0
        self.gesture_clip: dict[str, Any] | None = None
        self.gesture_time = 0.0
        self._rng = random.Random(seed if seed is not None else str(project.get("id", project.get("name", "rigm"))))
        self._next_blink = self._rng.uniform(2.0, 4.8)
        self._blink_time = -1.0
        self._next_fidget = self._rng.uniform(2.8, 5.2)
        self._fidget_time = -1.0
        self._fidget_duration = 0.55
        self._fidget_joint: str | None = None
        self._fidget_amplitude = 0.0
        self._transition_from: dict[str, Any] | None = None
        self._transition_elapsed = 0.4
        self._transition_duration = 0.32
        self._pace_phase = 0.0
        self._pose: dict[str, Any] = {
            "state": self.state,
            "rotations": {},
            "layer_visibility": {},
            "facial_parameters": {"eye_open": 1.0, "mouth_open": 0.0, "gaze_x": 0.0, "gaze_y": 0.0},
            "translation": (0.0, 0.0),
        }

    def _joint(self, *names: str) -> str | None:
        lowered = tuple(name.casefold() for name in names)
        for joint in self.joints:
            label = str(joint.get("name", "")).casefold()
            if any(name in label for name in lowered):
                return str(joint["id"])
        return None

    def _clip(self, *names: str) -> dict[str, Any] | None:
        wanted = {name.casefold() for name in names}
        return next((clip for clip in self.clips if str(clip.get("name", "")).casefold() in wanted), None)

    @property
    def pose(self) -> dict[str, Any]:
        """Current ephemeral pose, suitable for render_project_pose()."""
        return {
            "state": self._pose["state"],
            "rotations": dict(self._pose["rotations"]),
            "layer_visibility": dict(self._pose["layer_visibility"]),
            "facial_parameters": dict(self._pose["facial_parameters"]),
            "translation": tuple(self._pose.get("translation", (0.0, 0.0))),
        }

    def dispatch(self, event: dict[str, Any]) -> dict[str, Any]:
        """Apply one structured public event and return the new runtime state.

        Supported event types: ``state``, ``speech``, ``viseme``, ``gaze``,
        ``gesture``, and ``parameter``. Speech providers should map their own
        phoneme/viseme IDs into normalized mouth shape values before dispatch.
        """
        if not isinstance(event, dict):
            raise ProjectError("El evento de comportamiento debe ser un objeto JSON.")
        event_type = str(event.get("type", "")).casefold()
        if event_type == "state":
            state = str(event.get("state", "")).casefold()
            if state not in BEHAVIOR_STATES:
                raise ProjectError(f"Estado desconocido. Usa: {', '.join(BEHAVIOR_STATES)}.")
            self._change_state(state)
            if "intensity" in event:
                self.intensity = _clamp(event["intensity"])
            if "valence" in event:
                self.valence = _clamp(event["valence"], -1.0, 1.0)
        elif event_type == "parameter":
            name = str(event.get("name", "")).casefold()
            value = float(event.get("value", 0.0))
            if name == "intensity":
                self.intensity = _clamp(value)
            elif name == "valence":
                self.valence = _clamp(value, -1.0, 1.0)
            else:
                raise ProjectError(f"Parámetro de comportamiento no reconocido: {name}.")
        elif event_type == "gaze":
            self.gaze_input = (_clamp(event.get("x", 0.0), -1.0, 1.0), _clamp(event.get("y", 0.0), -1.0, 1.0))
        elif event_type == "speech":
            self._change_state("speaking")
            self.speech_input = {
                "mouth_open": _clamp(event.get("mouth_open") if event.get("mouth_open") is not None else event.get("level", 0.0)),
                "mouth_round": _clamp(event.get("mouth_round") if event.get("mouth_round") is not None else 0.0),
                "mouth_wide": _clamp(event.get("mouth_wide") if event.get("mouth_wide") is not None else 0.0),
            }
            self.speech_input_until = self.time + 0.25
        elif event_type == "viseme":
            self._change_state("speaking")
            shape = str(event.get("shape", "open")).casefold()
            defaults = {
                "silence": (0.0, 0.0, 0.0), "closed": (0.0, 0.0, 0.0),
                "open": (0.72, 0.0, 0.25), "wide": (0.48, 0.0, 1.0),
                "round": (0.55, 1.0, 0.0), "teeth": (0.32, 0.0, 0.8),
            }
            if shape not in defaults:
                raise ProjectError(f"Visema normalizado desconocido: {shape}.")
            opening, roundness, width = defaults[shape]
            self.speech_input = {
                "mouth_open": _clamp(event.get("mouth_open") if event.get("mouth_open") is not None else opening),
                "mouth_round": _clamp(event.get("mouth_round") if event.get("mouth_round") is not None else roundness),
                "mouth_wide": _clamp(event.get("mouth_wide") if event.get("mouth_wide") is not None else width),
            }
            self.speech_input_until = self.time + max(0.04, float(event.get("duration", 0.12)))
        elif event_type == "gesture":
            name = str(event.get("name", ""))
            clip = self._clip(name)
            if clip is None:
                raise ProjectError(f"No hay un clip de gesto llamado «{name}» en el proyecto.")
            self.gesture_clip = clip
            self.gesture_time = 0.0
        elif event_type == "gesture_end":
            self.gesture_clip = None
        else:
            raise ProjectError("Tipo de evento no reconocido. Usa state, parameter, gaze, speech, viseme, gesture o gesture_end.")
        return self.pose

    def _change_state(self, state: str) -> None:
        if state == self.state:
            return
        self._transition_from = self.pose
        self._transition_elapsed = 0.0
        self.state = state
        self.state_time = 0.0
        if state in {"restless", "thinking", "waiting"}:
            self._next_fidget = self._rng.uniform(1.8, 4.0)
            self._fidget_time = -1.0

    def update(self, dt: float) -> dict[str, Any]:
        """Advance behavior by elapsed seconds and return the blended pose."""
        dt = max(0.0, min(float(dt), 0.25))
        self._dt = dt
        self.time += dt
        self.state_time += dt
        if self.gesture_clip:
            self.gesture_time += dt
            duration = max(1, int(self.gesture_clip.get("duration", 1))) / max(1, int(self.gesture_clip.get("fps", 30)))
            if self.gesture_clip.get("loop"):
                self.gesture_time %= duration
            elif self.gesture_time >= duration:
                self.gesture_clip = None

        self._update_blink(dt)
        target = self._evaluate(dt)
        live_mouth = {key: target["facial_parameters"][key] for key in ("mouth_open", "mouth_round", "mouth_wide")}
        if self._transition_from is not None:
            self._transition_elapsed += dt
            amount = _clamp(self._transition_elapsed / self._transition_duration)
            amount = amount * amount * (3.0 - 2.0 * amount)
            target = self._blend(self._transition_from, target, amount)
            # TTS visemes are timestamped cues; do not smear a short mouth pose
            # into a state transition or it may disappear before becoming visible.
            target["facial_parameters"].update(live_mouth)
            if amount >= 1.0:
                self._transition_from = None
        self._pose = target
        return self.pose

    @staticmethod
    def _blend(start: dict[str, Any], end: dict[str, Any], amount: float) -> dict[str, Any]:
        rotations = {}
        for joint_id in set(start["rotations"]) | set(end["rotations"]):
            a = float(start["rotations"].get(joint_id, 0.0))
            b = float(end["rotations"].get(joint_id, 0.0))
            rotations[joint_id] = a + (b - a) * amount
        facial = {}
        for key in set(start["facial_parameters"]) | set(end["facial_parameters"]):
            a = float(start["facial_parameters"].get(key, 0.0))
            b = float(end["facial_parameters"].get(key, 0.0))
            facial[key] = a + (b - a) * amount
        visibility = start["layer_visibility"] if amount < 0.5 else end["layer_visibility"]
        start_offset = start.get("translation", (0.0, 0.0))
        end_offset = end.get("translation", (0.0, 0.0))
        translation = tuple(float(a) + (float(b) - float(a)) * amount for a, b in zip(start_offset, end_offset))
        return {"state": end["state"], "rotations": rotations,
                "layer_visibility": dict(visibility), "facial_parameters": facial, "translation": translation}

    def _evaluate(self, dt: float) -> dict[str, Any]:
        rotations: dict[str, float] = {}
        visibility: dict[str, bool] = {}
        if self.base_clip:
            fps = max(1, int(self.base_clip.get("fps", 30)))
            duration = max(1, int(self.base_clip.get("duration", fps)))
            frame = (self.time * fps) % duration if self.base_clip.get("loop", True) else min(self.time * fps, duration)
            rotations.update(sample_clip(self.base_clip, frame))
            visibility.update(sample_layer_visibility(self.base_clip, frame))

        # Quiet, asymmetric motion keeps the idle alive without shaking every joint.
        torso = self.semantic_joints["torso"]
        head = self.semantic_joints["head"]
        neck = self.semantic_joints["neck"]
        left_shoulder = self.semantic_joints["left_shoulder"]
        right_shoulder = self.semantic_joints["right_shoulder"]
        left_elbow = self.semantic_joints["left_elbow"]
        right_elbow = self.semantic_joints["right_elbow"]
        left_hip = self.semantic_joints["left_hip"]
        right_hip = self.semantic_joints["right_hip"]
        left_knee = self.semantic_joints["left_knee"]
        right_knee = self.semantic_joints["right_knee"]
        intensity = self.intensity
        if torso and self.state != "restless":
            rotations[torso] = rotations.get(torso, 0.0) + 0.32 * math.sin(self.time * 1.2) * (0.35 + intensity)
        if head:
            rotations[head] = rotations.get(head, 0.0) + 0.42 * math.sin(self.time * 0.72 + 0.7)
        gaze_x, gaze_y = self.gaze_input
        nod = 0.0
        translation_x = 0.0
        gait_bob = 0.0
        if self.state == "listening":
            gaze_x = gaze_x * 0.55 + 0.12 * math.sin(self.state_time * 0.37)
            gaze_y = gaze_y * 0.5 - 0.06
            nod = max(0.0, math.sin(self.state_time * 1.15 - 0.8)) ** 8 * 3.8
            if torso:
                rotations[torso] -= 0.55 + 0.3 * math.sin(self.state_time * 0.42)
            if left_shoulder:
                rotations[left_shoulder] += 1.15 * math.sin(self.state_time * 0.8)
            if right_shoulder:
                rotations[right_shoulder] += 0.9 * math.sin(self.state_time * 0.67 + 1.1)
        elif self.state == "thinking":
            glance = math.sin(self.state_time * 0.48 + 0.3)
            gaze_x = 0.9 * glance
            gaze_y = -0.52 + 0.16 * math.sin(self.state_time * 0.31)
            if head:
                rotations[head] = rotations.get(head, 0.0) - 2.8 * (0.3 + intensity) + 1.2 * math.sin(self.state_time * 0.42)
            if torso:
                rotations[torso] -= 0.35 + 0.25 * math.sin(self.state_time * 0.38)
            if neck:
                rotations[neck] = rotations.get(neck, 0.0) + 0.35 * math.sin(self.state_time * 0.27)
        elif self.state == "speaking":
            nod = max(0.0, math.sin(self.state_time * 2.1)) ** 5 * 1.25
            speech_energy = self.speech_input["mouth_open"] if self.speech_input and self.time <= self.speech_input_until else 0.32
            if left_shoulder:
                rotations[left_shoulder] += speech_energy * (3.0 + 3.0 * math.sin(self.state_time * 1.7))
            if right_shoulder:
                rotations[right_shoulder] -= speech_energy * (2.5 + 2.5 * math.sin(self.state_time * 1.4 + 1.8))
        elif self.state == "restless":
            self._pace_phase += dt * math.tau * (1.35 + 0.9 * intensity)
            stride = math.sin(self._pace_phase)
            lift_left = max(0.0, math.cos(self._pace_phase * 2.0))
            lift_right = max(0.0, -math.cos(self._pace_phase * 2.0))
            gaze_x = 0.24 * math.sin(self.state_time * 1.1)
            if left_hip:
                rotations[left_hip] = rotations.get(left_hip, 0.0) + 7.0 * stride
            if right_hip:
                rotations[right_hip] = rotations.get(right_hip, 0.0) - 7.0 * stride
            if left_knee:
                rotations[left_knee] = rotations.get(left_knee, 0.0) + 4.5 * lift_left
            if right_knee:
                rotations[right_knee] = rotations.get(right_knee, 0.0) + 4.5 * lift_right
            if left_shoulder:
                rotations[left_shoulder] = rotations.get(left_shoulder, 0.0) - 4.0 * stride
            if right_shoulder:
                rotations[right_shoulder] = rotations.get(right_shoulder, 0.0) + 4.0 * stride
            if left_elbow:
                rotations[left_elbow] = rotations.get(left_elbow, 0.0) + 2.2 + 1.8 * math.sin(self._pace_phase + 0.6)
            if right_elbow:
                rotations[right_elbow] = rotations.get(right_elbow, 0.0) + 2.2 - 1.8 * math.sin(self._pace_phase + 0.6)
            gait_bob = -2.2 * max(lift_left, lift_right)
            translation_x = math.sin(self.state_time * 0.62) * 0.055 * float(self.project.get("image_size", [1000])[0])

        if head:
            rotations[head] = rotations.get(head, 0.0) + nod
        if neck:
            rotations[neck] = rotations.get(neck, 0.0) + 0.22 * math.sin(self.time * 0.65)
        self._update_fidget(rotations, dt)

        if self.gesture_clip:
            clip = self.gesture_clip
            frame = self.gesture_time * max(1, int(clip.get("fps", 30)))
            rotations.update(sample_clip(clip, frame))
            visibility.update(sample_layer_visibility(clip, frame))

        if self.state == "speaking":
            if self.speech_input and self.time <= self.speech_input_until:
                mouth = dict(self.speech_input)
            else:
                # A visual fallback for previews without an attached TTS stream.
                pulse = max(0.0, math.sin(self.state_time * math.tau * 2.7 + 0.4))
                mouth = {"mouth_open": 0.08 + 0.52 * pulse, "mouth_round": 0.0, "mouth_wide": 0.45}
        else:
            mouth = {"mouth_open": 0.0, "mouth_round": 0.0, "mouth_wide": 0.0}
        facial = {
            "eye_open": self._eye_open(),
            "gaze_x": gaze_x,
            "gaze_y": gaze_y,
            "brow_raise": _clamp(max(0.0, self.valence) * 0.35 + (0.18 if self.state == "listening" else 0.0)),
            **mouth,
        }
        return {"state": self.state, "rotations": rotations,
                "layer_visibility": visibility, "facial_parameters": facial,
                "translation": (translation_x, gait_bob)}

    def _update_blink(self, dt: float) -> None:
        if self._blink_time < 0:
            if self.time >= self._next_blink:
                self._blink_time = 0.0
                self._next_blink = self.time + self._rng.uniform(2.1, 5.0)
        else:
            self._blink_time += dt
            if self._blink_time >= 0.18:
                self._blink_time = -1.0

    def _eye_open(self) -> float:
        if self._blink_time < 0:
            return 1.0
        phase = _clamp(self._blink_time / 0.18)
        return _clamp(1.0 - math.sin(math.pi * phase))

    def _update_fidget(self, rotations: dict[str, float], dt: float) -> None:
        enabled = self.state == "restless" or (self.state in {"thinking", "waiting"} and self.state_time > 2.0)
        if not enabled:
            self._fidget_time = -1.0
            return
        if self._fidget_time < 0:
            self._next_fidget -= dt
            if self._next_fidget <= 0:
                candidates = [joint for joint in (self.semantic_joints["head"], self.semantic_joints["left_shoulder"], self.semantic_joints["right_shoulder"]) if joint]
                if not candidates:
                    return
                self._fidget_joint = self._rng.choice(candidates)
                self._fidget_amplitude = self._rng.choice((-1.0, 1.0)) * (0.45 + 1.35 * self.intensity)
                self._fidget_time = 0.0
                self._next_fidget = self._rng.uniform(2.7, 5.5) / (0.7 + self.intensity)
        else:
            self._fidget_time += dt
            progress = _clamp(self._fidget_time / self._fidget_duration)
            if self._fidget_joint:
                envelope = math.sin(math.pi * progress) ** 2
                rotations[self._fidget_joint] = rotations.get(self._fidget_joint, 0.0) + self._fidget_amplitude * envelope
            if progress >= 1.0:
                self._fidget_time = -1.0


def motion_event_from_agent(event: dict[str, Any]) -> dict[str, Any]:
    """Normalize common agent/TTS status events to the runtime contract.

    This intentionally accepts public activity/status signals, not private
    reasoning traces. Speech providers should separately map their viseme IDs.
    """
    kind = str(event.get("type", "")).casefold()
    if kind in {"agent.status", "agent.phase"}:
        value = str(event.get("state", event.get("phase", "thinking"))).casefold()
        aliases = {"processing": "thinking", "tool_running": "thinking", "waiting_for_user": "listening", "responding": "speaking", "failed": "error", "nervous": "restless", "anxious": "restless", "fidgeting": "restless"}
        return {"type": "state", "state": aliases.get(value, value), "intensity": event.get("intensity", 0.35), "valence": event.get("valence", 0.0)}
    if kind in {"tts.viseme", "speech.viseme"}:
        return {"type": "viseme", "shape": event.get("shape", "open"), "mouth_open": event.get("mouth_open"), "mouth_round": event.get("mouth_round"), "mouth_wide": event.get("mouth_wide"), "duration": event.get("duration", 0.12)}
    if kind == "speech.prosody":
        return {"type": "speech", "level": event.get("energy", 0.0), "emphasis": event.get("emphasis", 0.0)}
    if kind == "speech.level":
        return {"type": "speech", "mouth_open": event.get("level", 0.0), "mouth_round": event.get("mouth_round", 0.0), "mouth_wide": event.get("mouth_wide", 0.0)}
    if kind == "agent.gesture":
        return {"type": "gesture", "name": event.get("name", "")}
    if kind == "agent.gaze":
        return {"type": "gaze", "x": event.get("x", 0.0), "y": event.get("y", 0.0)}
    if kind == "agent.intent":
        return {"type": "gesture", "name": event.get("intent", event.get("name", ""))}
    raise ProjectError(f"Evento externo no reconocido: {kind}.")
