"""Optional Qt WebEngine window for the bundled local VRM viewer."""

from __future__ import annotations

import json
import base64
import tempfile
import uuid
import sys
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QFile, QIODevice, QUrl, Qt, QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QComboBox, QDialog, QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout

# Qt WebEngine must be imported before QApplication is instantiated. Importing
# these lazily from Avatar3DDialog.__init__ is too late in the normal GUI flow.
try:
    from PyQt6.QtWebEngineCore import QWebEngineSettings, QWebEnginePage, QWebEngineScript
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebChannel import QWebChannel
    _WEBENGINE_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # keep the 2D editor usable without the optional extra
    QWebEngineSettings = QWebEnginePage = QWebEngineScript = QWebEngineView = QWebChannel = None
    _WEBENGINE_IMPORT_ERROR = exc

from .avatar_behavior import AvatarBehaviorPlanner
from .behavior import BEHAVIOR_STATES
from .project import ProjectError, load_project
from .avatar3d import attach_avatar


STATE_LABELS = {
    "idle": "Reposo", "listening": "Escuchando", "thinking": "Procesando",
    "speaking": "Hablando", "waiting": "Esperando", "restless": "Nerviosa · paseando", "error": "Error",
}


def webengine_available() -> bool:
    return _WEBENGINE_IMPORT_ERROR is None


class Avatar3DDialog(QDialog):
    """Interactive local GLB/VRM viewer and host for the browser-side auto-rigger."""

    autorig_saved = pyqtSignal(object)
    autorig_failed = pyqtSignal(str)

    def __init__(
        self, model_path: str | Path, model_name: str = "Avatar 3D", parent=None,
        *, project_path: str | Path | None = None, expected_mtime_ns: int | None = None,
        allow_autorig: bool = False, forward_axis: str = "+Z",
    ) -> None:
        if _WEBENGINE_IMPORT_ERROR is not None:
            exc = _WEBENGINE_IMPORT_ERROR
            raise ProjectError(
                "No se pudo importar Qt WebEngine en este intérprete:\n"
                f"{sys.executable}\nDetalle: {exc}\n"
                "Instálalo en este mismo entorno con:\n"
                f"{sys.executable} -m pip install -e '.[avatar3d]'"
            ) from exc
        super().__init__(parent)
        self.model_path = Path(model_path).expanduser().resolve()
        self.project_path = Path(project_path).expanduser().resolve() if project_path else None
        self.expected_mtime_ns = expected_mtime_ns
        self.allow_autorig = allow_autorig
        self.forward_axis = forward_axis if forward_axis in {"+Z", "-Z", "+X", "-X"} else "+Z"
        self.page_ready = False
        self.model_ready = False
        self.autorig_requested = False
        self.planner = AvatarBehaviorPlanner()
        self.setWindowTitle(f"RigM · Avatar 3D · {model_name}")
        self.resize(980, 760)
        self.setMinimumSize(540, 430)
        self.web = QWebEngineView(self)
        self.web.setMinimumHeight(260)

        owner = self
        class DiagnosticPage(QWebEnginePage):
            def javaScriptConsoleMessage(self, level, message, line, source):
                # PyQt6 passes JavaScriptConsoleMessageLevel (an enum), not an
                # int. Never let an exception escape a Qt virtual callback:
                # PyQt may abort the whole process for an uncaught callback error.
                try:
                    severity = getattr(level, "value", level)
                    if severity in ("error", "Error"):
                        is_error = True
                    else:
                        is_error = int(severity) >= 2
                    if is_error:
                        owner.message.setText(f"Visor 3D · {message} (línea {line})")
                except Exception as exc:
                    try:
                        owner.message.setText(f"Error interno del visor · {message} · {exc}")
                    except Exception:
                        pass

        self.page = DiagnosticPage(self.web)
        self.web.setPage(self.page)
        channel_script_file = QFile(":/qtwebchannel/qwebchannel.js")
        if not channel_script_file.open(QIODevice.OpenModeFlag.ReadOnly):
            raise ProjectError("Qt WebEngine no pudo abrir el adaptador local de comunicación (qwebchannel.js).")
        channel_script = QWebEngineScript()
        channel_script.setName("RigM Qt WebChannel bootstrap")
        channel_script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        channel_script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        channel_script.setRunsOnSubFrames(False)
        channel_script.setSourceCode(bytes(channel_script_file.readAll()).decode("utf-8"))
        self.page.scripts().insert(channel_script)
        channel_script_file.close()
        self.bridge = Avatar3DBridge(self.project_path, self.expected_mtime_ns, self) if self.project_path else None
        self.channel = QWebChannel(self.page)
        if self.bridge:
            self.channel.registerObject("rigmHost", self.bridge)
            self.bridge.saved.connect(self._autorig_saved)
            self.bridge.failed.connect(self._autorig_failed)
            self.bridge.progress.connect(self._on_progress)
        self.page.setWebChannel(self.channel)
        settings = self.web.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.web.loadFinished.connect(self._on_page_loaded)

        controls = QHBoxLayout()
        self.state_combo = QComboBox()
        for state in BEHAVIOR_STATES:
            self.state_combo.addItem(STATE_LABELS[state], state)
        self.state_combo.currentIndexChanged.connect(self._send_state)
        controls.addWidget(QLabel("Conducta"))
        controls.addWidget(self.state_combo)
        self.intensity_slider = QSlider(Qt.Orientation.Horizontal)
        self.intensity_slider.setRange(0, 100)
        self.intensity_slider.setValue(35)
        self.intensity_slider.valueChanged.connect(self._send_intensity)
        intensity_controls = QHBoxLayout()
        intensity_controls.addWidget(QLabel("Intensidad"))
        intensity_controls.addWidget(self.intensity_slider, 1)
        self.autorig_button = None
        action_controls = QHBoxLayout()
        if self.allow_autorig:
            self.autorig_button = QPushButton("Crear esqueleto sin Blender")
            self.autorig_button.setToolTip("Analiza la geometría por lotes y propone huesos y pesos.")
            self.autorig_button.clicked.connect(self.request_autorig)
            action_controls.addWidget(self.autorig_button)
        self.skeleton_button = QPushButton("Ocultar esqueleto")
        self.skeleton_button.setCheckable(True)
        self.skeleton_button.setChecked(True)
        self.skeleton_button.setEnabled(False)
        self.skeleton_button.toggled.connect(self._toggle_skeleton)
        action_controls.addWidget(self.skeleton_button)

        bone_controls = QHBoxLayout()
        bone_controls.addWidget(QLabel("Hueso"))
        self.bone_combo = QComboBox()
        self.bone_combo.setMinimumWidth(110)
        self.bone_combo.setMaximumWidth(180)
        self.bone_combo.setEnabled(False)
        self.bone_combo.currentTextChanged.connect(self._select_bone)
        bone_controls.addWidget(self.bone_combo)
        self.bone_spins: list[QDoubleSpinBox] = []
        for axis in ("X", "Y", "Z"):
            bone_controls.addWidget(QLabel(axis))
            spin = QDoubleSpinBox()
            spin.setRange(-90.0, 90.0)
            spin.setDecimals(1)
            spin.setSingleStep(2.0)
            spin.setSuffix("°")
            spin.setMaximumWidth(78)
            spin.setEnabled(False)
            spin.valueChanged.connect(self._apply_bone_rotation)
            bone_controls.addWidget(spin)
            self.bone_spins.append(spin)
        self.reset_bone_button = QPushButton("Restaurar")
        self.reset_bone_button.setEnabled(False)
        self.reset_bone_button.clicked.connect(self._reset_selected_bone)
        action_controls.addWidget(self.reset_bone_button)
        self.save_edit_button = QPushButton("Guardar rig/pose editados")
        self.save_edit_button.setEnabled(False)
        self.save_edit_button.clicked.connect(self.save_edited_model)
        action_controls.addWidget(self.save_edit_button)
        position_controls = QHBoxLayout()
        position_controls.addWidget(QLabel("Mover pivote local"))
        self.position_spins: list[QDoubleSpinBox] = []
        for axis in ("X", "Y", "Z"):
            position_controls.addWidget(QLabel(axis))
            spin = QDoubleSpinBox()
            spin.setRange(-0.5, 0.5)
            spin.setDecimals(3)
            spin.setSingleStep(0.01)
            spin.setMaximumWidth(86)
            spin.setEnabled(False)
            spin.valueChanged.connect(self._apply_bone_position)
            position_controls.addWidget(spin)
            self.position_spins.append(spin)
        self.message = QLabel("Cargando visor local… · el selector de conducta sirve para probar estados; aún no recibe eventos en vivo de Meido.")
        self.message.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self.web, 1)
        layout.addLayout(controls)
        layout.addLayout(intensity_controls)
        layout.addLayout(action_controls)
        layout.addLayout(bone_controls)
        layout.addLayout(position_controls)
        layout.addWidget(self.message)

        bundle = Path(__file__).resolve().parent / "viewer3d_static" / "index.html"
        if not bundle.is_file():
            raise ProjectError("No encuentro el visor 3D compilado. Ejecuta `npm install && npm run build` dentro de viewer3d/.")
        self.web.setUrl(QUrl.fromLocalFile(str(bundle)))

    def _run(self, script: str, callback=None) -> None:
        self.web.page().runJavaScript(script, callback or (lambda _result: None))

    def _on_page_loaded(self, ok: bool) -> None:
        if not ok:
            self.message.setText("No se pudo abrir el visor 3D local. Revisa Qt WebEngine y WebGL en el sistema.")
            return
        self.page_ready = True
        model_url = self.model_path.as_uri()
        name = self.windowTitle().split("·")[-1].strip()
        script = """(function start(tries) {
          if (window.rigmAvatar) {
            window.rigmAvatar.loadModel(%s, %s).then(info => {
              window.rigmAvatar.status(`Modelo ${info.format.toUpperCase()} cargado · ${info.animations.length} clips`);
              window.dispatchEvent(new Event('rigm-model-ready'));
            }).catch(error => {
              window.rigmAvatar.status(`Error cargando modelo: ${error.message}`);
            });
            return;
          }
          if (tries <= 0) {
            const detail = document.querySelector('#model-detail');
            if (detail) detail.textContent = 'El visor 3D no inició. Comprueba WebGL y vuelve a abrir la vista.';
            return;
          }
          setTimeout(() => start(tries - 1), 50);
        })(100);""" % (json.dumps(model_url), json.dumps(name))
        self._run(script)
        self._poll_model_load(100)

    def request_autorig(self) -> None:
        """Request geometry-based rig creation in the local Three.js viewer."""
        self.autorig_requested = True
        if self.autorig_button:
            self.autorig_button.setEnabled(False)
            self.autorig_button.setText("Calculando pesos…")
        self.message.setText("Esperando a que termine de cargar el modelo…" if not self.model_ready else "Preparando el análisis de pesos…")
        if self.page_ready and self.model_ready:
            self._run("window.rigmAvatar?.autoRig(" + json.dumps(self.forward_axis) + ").catch(() => {});")

    def _poll_model_load(self, attempts: int) -> None:
        if not self.page_ready or self.model_ready:
            return
        self._run("window.rigmAvatar?.getInfo() || null", lambda info: self._model_load_status(info, attempts))

    def _model_load_status(self, info: Any, attempts: int) -> None:
        if isinstance(info, dict) and info.get("loaded"):
            self._model_load_finished({**info, "ok": True})
            return
        if isinstance(info, dict) and info.get("loadState") == "error":
            self._model_load_finished({**info, "ok": False, "error": info.get("loadError")})
            return
        if attempts <= 0:
            self._model_load_finished({"ok": False, "error": "El visor no confirmó la carga del modelo en 20 segundos."})
            return
        QTimer.singleShot(200, lambda: self._poll_model_load(attempts - 1))

    def _model_load_finished(self, result: Any) -> None:
        if not isinstance(result, dict) or not result.get("ok"):
            detail = result.get("error", "El visor no devolvió información del modelo.") if isinstance(result, dict) else "El visor no devolvió información del modelo."
            self.message.setText(f"No se pudo mostrar el modelo: {detail}")
            self.autorig_requested = False
            if self.autorig_button:
                self.autorig_button.setEnabled(True)
                self.autorig_button.setText("Reintentar auto-rig")
            return
        self.model_ready = True
        self.message.setText(
            f"Modelo cargado · {result.get('meshes', 0)} mallas · "
            f"{len(result.get('bones', []))} huesos · {len(result.get('animations', []))} clips. "
            "El selector de conducta prueba estados; Meido aún no está conectado en vivo."
        )
        self._send_state()
        self._receive_bones(result, 0)
        if self.autorig_requested:
            self._run("window.rigmAvatar?.autoRig(" + json.dumps(self.forward_axis) + ").catch(() => {});")

    def _on_progress(self, message: str) -> None:
        self.message.setText(str(message))

    def _autorig_saved(self, payload: dict[str, Any]) -> None:
        self.autorig_requested = False
        self.model_ready = True
        if self.autorig_button:
            self.autorig_button.setEnabled(False)
            self.autorig_button.setText("Esqueleto guardado")
        self.message.setText("GLB actualizado y guardado en el proyecto. Revisa la deformación antes de usarlo.")
        self.autorig_saved.emit(payload)

    def _autorig_failed(self, message: str) -> None:
        self.autorig_requested = False
        if self.autorig_button:
            self.autorig_button.setEnabled(True)
            self.autorig_button.setText("Reintentar auto-rig")
        self.message.setText(message)
        self.autorig_failed.emit(message)

    def _send_state(self, _index: int = -1) -> None:
        try:
            plan = self.planner.dispatch({"type": "state", "state": self.state_combo.currentData(), "intensity": self.intensity_slider.value() / 100})
        except ProjectError as exc:
            self.message.setText(str(exc))
            return
        self._run("window.rigmAvatar?.setPlan(" + json.dumps(plan, ensure_ascii=False) + ");")

    def _send_intensity(self, value: int) -> None:
        plan = self.planner.dispatch({"type": "parameter", "name": "intensity", "value": value / 100})
        self._run("window.rigmAvatar?.setPlan(" + json.dumps(plan, ensure_ascii=False) + ");")

    def _refresh_bones(self, attempts: int = 0) -> None:
        if not self.page_ready:
            return
        self._run("window.rigmAvatar?.getInfo()", lambda info: self._receive_bones(info, attempts))

    def _receive_bones(self, info: Any, attempts: int) -> None:
        bones = info.get("bones", []) if isinstance(info, dict) else []
        if not bones and attempts > 0:
            QTimer.singleShot(200, lambda: self._refresh_bones(attempts - 1))
            return
        current = self.bone_combo.currentText()
        self.bone_combo.blockSignals(True)
        self.bone_combo.clear()
        self.bone_combo.addItems([name for name in bones if name])
        if current:
            index = self.bone_combo.findText(current)
            if index >= 0:
                self.bone_combo.setCurrentIndex(index)
        self.bone_combo.blockSignals(False)
        enabled = bool(self.bone_combo.count())
        self.skeleton_button.setEnabled(enabled)
        self.bone_combo.setEnabled(enabled)
        for spin in self.bone_spins:
            spin.setEnabled(enabled)
        for spin in self.position_spins:
            spin.setEnabled(enabled and info.get("format") == "glb")
        self.reset_bone_button.setEnabled(enabled)
        self.save_edit_button.setEnabled(enabled and self.bridge is not None and info.get("format") == "glb")
        if enabled:
            self._select_bone(self.bone_combo.currentText())
        elif isinstance(info, dict) and info.get("loaded"):
            self.message.setText("El modelo cargó, pero no contiene huesos. Crea el auto-rig para inspeccionarlos y editarlos.")

    def _toggle_skeleton(self, visible: bool) -> None:
        self.skeleton_button.setText("Ocultar esqueleto" if visible else "Mostrar esqueleto")
        self._run("window.rigmAvatar?.setSkeletonVisible(" + ("true" if visible else "false") + ");")

    def _select_bone(self, name: str) -> None:
        if not name:
            return
        self._run("window.rigmAvatar?.getBoneRotation(" + json.dumps(name) + ")", self._receive_bone_rotation)
        self._run("window.rigmAvatar?.getBonePosition(" + json.dumps(name) + ")", self._receive_bone_position)

    def _receive_bone_rotation(self, values: Any) -> None:
        if not isinstance(values, list) or len(values) != 3:
            return
        for spin, value in zip(self.bone_spins, values):
            spin.blockSignals(True)
            spin.setValue(float(value))
            spin.blockSignals(False)

    def _apply_bone_rotation(self, _value: float = 0.0) -> None:
        name = self.bone_combo.currentText()
        if not name:
            return
        rotation = [spin.value() for spin in self.bone_spins]
        self._run("window.rigmAvatar?.setBoneRotation(" + json.dumps(name) + "," + json.dumps(rotation) + ");")

    def _receive_bone_position(self, values: Any) -> None:
        if not isinstance(values, list) or len(values) != 3:
            return
        for spin, value in zip(self.position_spins, values):
            spin.blockSignals(True)
            spin.setValue(float(value))
            spin.blockSignals(False)

    def _apply_bone_position(self, _value: float = 0.0) -> None:
        name = self.bone_combo.currentText()
        if name:
            position = [spin.value() for spin in self.position_spins]
            self._run("window.rigmAvatar?.setBonePosition(" + json.dumps(name) + "," + json.dumps(position) + ");")

    def _reset_selected_bone(self) -> None:
        name = self.bone_combo.currentText()
        if name:
            self._run("window.rigmAvatar?.resetBone(" + json.dumps(name) + ");")
            for spin in self.bone_spins:
                spin.blockSignals(True)
                spin.setValue(0)
                spin.blockSignals(False)
            for spin in self.position_spins:
                spin.blockSignals(True)
                spin.setValue(0)
                spin.blockSignals(False)

    def save_edited_model(self) -> None:
        if self.page_ready:
            self._run("window.rigmAvatar?.saveEditedModel().catch(() => {});")

    def dispatch_agent_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Update the avatar from a public Meido status/TTS event."""
        plan = self.planner.dispatch(event)
        self._run("window.rigmAvatar?.setPlan(" + json.dumps(plan, ensure_ascii=False) + ");")
        return plan


class Avatar3DBridge(QObject):
    """Receive a chunked GLB export from the bundled browser runtime."""

    saved = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, project_path: Path, expected_mtime_ns: int | None, parent=None):
        super().__init__(parent)
        self.project_path = project_path
        self.expected_mtime_ns = expected_mtime_ns
        self.transfers: dict[str, dict[str, Any]] = {}

    @pyqtSlot(str, result=bool)
    def reportProgress(self, message: str) -> bool:
        self.progress.emit(message)
        return True

    @pyqtSlot(str, result=bool)
    def reportFailure(self, message: str) -> bool:
        self.failed.emit(message)
        return True

    @pyqtSlot(str, int, str, result=str)
    def beginAutoRig(self, model_name: str, total_bytes: int, report_json: str) -> str:
        try:
            if total_bytes < 20 or total_bytes > 512 * 1024 * 1024:
                raise ProjectError("El GLB generado tiene un tamaño inválido o supera 512 MB.")
            if self.project_path is None or not self.project_path.is_file():
                raise ProjectError("El proyecto RigM ya no está disponible.")
            if self.expected_mtime_ns is not None and self.project_path.stat().st_mtime_ns != self.expected_mtime_ns:
                raise ProjectError("El proyecto cambió externamente; recárgalo antes de guardar el esqueleto.")
            data = load_project(self.project_path)
            avatar = data.get("avatar3d", {})
            if not isinstance(avatar, dict) or avatar.get("format") != "glb":
                raise ProjectError("El proyecto ya no contiene el GLB que se está modificando.")
            report = json.loads(report_json)
            if not isinstance(report, dict):
                raise ProjectError("El informe del auto-rig no tiene formato válido.")
            transfer_id = uuid.uuid4().hex
            handle = tempfile.NamedTemporaryFile(prefix="rigm-autorig-", suffix=".glb", delete=False)
            self.transfers[transfer_id] = {
                "handle": handle, "path": Path(handle.name), "size": total_bytes,
                "written": 0, "name": model_name, "report": report,
                "source_path": avatar.get("source_path", avatar["path"]),
                "source_sha256": avatar.get("source_sha256", avatar["sha256"]),
            }
            return transfer_id
        except (OSError, ValueError, KeyError, ProjectError) as exc:
            self.failed.emit(str(exc))
            return ""

    @pyqtSlot(str, str, result=bool)
    def writeAutoRigChunk(self, transfer_id: str, encoded_chunk: str) -> bool:
        try:
            transfer = self.transfers[transfer_id]
            chunk = base64.b64decode(encoded_chunk, validate=True)
            transfer["written"] += len(chunk)
            if transfer["written"] > transfer["size"]:
                raise ProjectError("El visor envió más bytes de los declarados para el GLB.")
            transfer["handle"].write(chunk)
            return True
        except (KeyError, ValueError, OSError, ProjectError) as exc:
            self._discard_transfer(transfer_id)
            self.failed.emit(str(exc))
            return False

    @pyqtSlot(str, result=bool)
    def finishAutoRig(self, transfer_id: str) -> bool:
        transfer = self.transfers.get(transfer_id)
        if not transfer:
            self.failed.emit("No encuentro la transferencia del GLB generado.")
            return False
        try:
            transfer["handle"].flush()
            transfer["handle"].close()
            if transfer["written"] != transfer["size"]:
                raise ProjectError("La transferencia del GLB quedó incompleta.")
            if self.expected_mtime_ns is not None and self.project_path.stat().st_mtime_ns != self.expected_mtime_ns:
                raise ProjectError("El proyecto cambió externamente durante el auto-rig; no guardé el resultado.")
            report = transfer["report"]
            result = attach_avatar(self.project_path, transfer["path"], actor="user", extra_metadata={
                "name": transfer["name"],
                "source_path": transfer["source_path"], "source_sha256": transfer["source_sha256"],
                "rigging": {"method": report.get("method", "rigm_threejs_geometry_autorig"), **report},
            })
            self.saved.emit(result)
            return True
        except (OSError, ValueError, KeyError, ProjectError) as exc:
            self.failed.emit(str(exc))
            return False
        finally:
            self._discard_transfer(transfer_id)

    @pyqtSlot(str)
    def cancelAutoRig(self, transfer_id: str) -> None:
        self._discard_transfer(transfer_id)

    def _discard_transfer(self, transfer_id: str) -> None:
        transfer = self.transfers.pop(transfer_id, None)
        if not transfer:
            return
        try:
            if not transfer["handle"].closed:
                transfer["handle"].close()
        finally:
            Path(transfer["path"]).unlink(missing_ok=True)

