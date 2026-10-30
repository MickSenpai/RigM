from __future__ import annotations

import math
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGraphicsScene,
    QGraphicsView,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QInputDialog,
    QLineEdit,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QButtonGroup,
    QScrollArea,
    QSplitter,
    QSpinBox,
)

from .project import (
    ProjectError, ProjectLibrary, analyze_mask_proposal, create_layer, create_project, extract_layer,
    accept_layer_mask_proposal, inpaint_layer_holes, load_project,
    propose_layer_masks, render_composite, save_project, validate_project,
    create_animation_clip, create_expression, import_decomposition_psd, set_expression_value, set_rotation_key,
    set_layer_visibility_key,
)
from .rig import generate_layer_mesh, posed_joint_positions, render_project_pose, sample_clip, sample_layer_visibility, set_vertex_weight, solve_two_bone_ik
from .behavior import BehaviorRuntime, BEHAVIOR_STATES
from .avatar3d import attach_avatar, autorig_glb, validate_project_avatar
from .avatar3d_viewer import Avatar3DDialog
from .export import export_bundle
from .decomposition import decompose_project_with_see_through
from PIL import Image as PILImage, ImageDraw

IMAGE_FILTER = "Imágenes (*.png *.jpg *.jpeg *.bmp *.webp)"
PROJECT_FILTER = "Proyecto RigM (*.rigm)"


class SegmentationWorker(QThread):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, project_path: Path, layer_id: str, positive_points, negative_points, box):
        super().__init__()
        self.project_path = project_path
        self.layer_id = layer_id
        self.positive_points = positive_points
        self.negative_points = negative_points
        self.box = box

    def run(self) -> None:
        try:
            result = propose_layer_masks(
                self.project_path,
                self.layer_id,
                self.positive_points,
                self.negative_points,
                self.box,
                actor="user",
            )
        except (OSError, ProjectError) as exc:
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)


class DecompositionWorker(QThread):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, project_path: Path, token: str | None, group_layers: bool = True, split_left_right: bool = False):
        super().__init__()
        self.project_path = project_path
        self.token = token
        self.group_layers = group_layers
        self.split_left_right = split_left_right

    def run(self) -> None:
        try:
            result = decompose_project_with_see_through(
                self.project_path, token=self.token, group_layers=self.group_layers,
                split_left_right=self.split_left_right,
            )
        except (OSError, ProjectError) as exc:
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)


class AvatarAutoRigWorker(QThread):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, project_path: Path, forward_axis: str, blender_path: str | None = None, expected_mtime_ns: int | None = None):
        super().__init__()
        self.project_path = project_path
        self.forward_axis = forward_axis
        self.blender_path = blender_path
        self.expected_mtime_ns = expected_mtime_ns

    def run(self) -> None:
        try:
            result = autorig_glb(self.project_path, blender_path=self.blender_path, forward_axis=self.forward_axis, actor="user", expected_mtime_ns=self.expected_mtime_ns)
        except (OSError, ProjectError) as exc:
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)


class RigCanvas(QGraphicsView):
    changed = pyqtSignal()
    joint_added = pyqtSignal(str)
    joint_selected = pyqtSignal(list)
    mask_changed = pyqtSignal(str)
    prompts_changed = pyqtSignal()
    ik_target_changed = pyqtSignal(float, float)

    def __init__(self) -> None:
        super().__init__()
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor("#151923"))
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setScene(QGraphicsScene(self))
        self.image: QPixmap | None = None
        self.composite_preview: QPixmap | None = None
        self.preview_enabled = False
        self.image_path: Path | None = None
        self.joints: list[dict[str, Any]] = []
        self.preview_joint_positions: dict[str, tuple[float, float]] | None = None
        self.bones: list[list[str]] = []
        self.selected: list[str] = []
        self.inserting = False
        self.dragging: str | None = None
        self.panning = False
        self.pan_at = QPointF()
        self._sequence = 1
        self.project_dir: Path | None = None
        self.project_metadata: dict[str, Any] = {}
        self.layers: list[dict[str, Any]] = []
        self.active_layer_id: str | None = None
        self.mask_image: PILImage.Image | None = None
        self.mask_overlay: QImage | None = None
        self.proposal_overlay: QImage | None = None
        self.mask_path: Path | None = None
        self.paint_mode = False
        self.painting = False
        self.erase_stroke = False
        self.last_paint_point: QPointF | None = None
        self.brush_size = 18
        self.prompt_mode: str | None = None
        self.positive_points: list[tuple[float, float]] = []
        self.negative_points: list[tuple[float, float]] = []
        self.prompt_box: tuple[float, float, float, float] | None = None
        self.box_start: QPointF | None = None
        self.drawing_box = False
        self.temporary_box: tuple[float, float, float, float] | None = None
        self.target_pick_mode = False
        self.ik_target: tuple[float, float] | None = None

    def set_image(self, path: Path) -> None:
        image = QPixmap(str(path))
        if image.isNull():
            raise ValueError("No pude leer esa imagen.")
        self.image = image
        self.composite_preview = None
        self.preview_enabled = False
        self.image_path = path.resolve()
        self.joints.clear()
        self.preview_joint_positions = None
        self.bones.clear()
        self.layers.clear()
        self.active_layer_id = None
        self.mask_image = None
        self.mask_overlay = None
        self.proposal_overlay = None
        self.mask_path = None
        self.project_dir = None
        self.project_metadata = {}
        self.selected.clear()
        self.prompt_mode = None
        self.positive_points.clear()
        self.negative_points.clear()
        self.prompt_box = None
        self.temporary_box = None
        self.drawing_box = False
        self.target_pick_mode = False
        self.ik_target = None
        self._sequence = 1
        image_rect = QRectF(0, 0, image.width(), image.height())
        self.scene().setSceneRect(image_rect)
        self.setSceneRect(image_rect)
        self.resetTransform()
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.changed.emit()
        self.joint_selected.emit([])
        self.prompts_changed.emit()
        self.viewport().update()

    def load_model(self, data: dict[str, Any], project_path: Path) -> None:
        image_path = Path(data["image"])
        if not image_path.is_absolute():
            image_path = (project_path.parent / image_path).resolve()
        self.set_image(image_path)
        self.joints = list(data.get("joints", []))
        self.bones = [list(edge) for edge in data.get("bones", [])]
        self.layers = list(data.get("layers", []))
        structural = {"format", "version", "image", "image_size", "joints", "bones", "layers"}
        self.project_metadata = {key: value for key, value in data.items()
                                 if key not in structural and not key.startswith("_")}
        self.project_dir = project_path.parent.resolve()
        self._sequence = 1 + max(
            (int(j["id"].split("-")[-1]) for j in self.joints
             if j["id"].startswith("joint-") and j["id"].split("-")[-1].isdigit()),
            default=0,
        )
        self.viewport().update()
        self.active_layer_id = self.layers[0]["id"] if self.layers else None
        self._load_active_mask()

    def model_data(self, image_reference: str | None = None) -> dict[str, Any]:
        if self.image_path is None:
            raise ValueError("Abre una imagen antes de guardar.")
        return {
            **self.project_metadata,
            "format": "rigm",
            "version": 1,
            "image": image_reference or str(self.image_path),
            "image_size": [self.image.width(), self.image.height()],
            "joints": self.joints,
            "bones": self.bones,
            "layers": self.layers,
        }

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, QColor("#151923"))
        image = self.composite_preview if self.preview_enabled and self.composite_preview else self.image
        if image:
            painter.drawPixmap(QPointF(0, 0), image)

    def drawForeground(self, painter: QPainter, _rect: QRectF) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.proposal_overlay is not None:
            painter.drawImage(QPointF(0, 0), self.proposal_overlay)
        elif self.mask_overlay is not None:
            painter.drawImage(QPointF(0, 0), self.mask_overlay)
        for x, y in self.positive_points:
            point = QPointF(x, y)
            painter.setPen(QPen(QColor("#40e890"), 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(point, 7, 7)
            painter.drawLine(point + QPointF(-5, 0), point + QPointF(5, 0))
            painter.drawLine(point + QPointF(0, -5), point + QPointF(0, 5))
        for x, y in self.negative_points:
            point = QPointF(x, y)
            painter.setPen(QPen(QColor("#ff647c"), 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(point, 7, 7)
            painter.drawLine(point + QPointF(-4, -4), point + QPointF(4, 4))
            painter.drawLine(point + QPointF(-4, 4), point + QPointF(4, -4))
        box = self.temporary_box or self.prompt_box
        if box:
            painter.setPen(QPen(QColor("#ffcf70"), 2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(box[0], box[1], box[2] - box[0], box[3] - box[1]))
        if self.ik_target:
            point = QPointF(*self.ik_target)
            painter.setPen(QPen(QColor("#ff8bfa"), 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(point, 9, 9)
            painter.drawLine(point + QPointF(-12, 0), point + QPointF(12, 0))
            painter.drawLine(point + QPointF(0, -12), point + QPointF(0, 12))
        joint_by_id = {joint["id"]: joint for joint in self.joints}
        posed_positions = self.preview_joint_positions if self.preview_enabled else None

        def joint_point(joint: dict[str, Any]) -> QPointF:
            x, y = (posed_positions or {}).get(
                joint["id"], (float(joint["x"]), float(joint["y"]))
            )
            return QPointF(x, y)

        painter.setPen(QPen(QColor("#6ee7d8"), 2.5))
        for a, b in self.bones:
            if a in joint_by_id and b in joint_by_id:
                painter.drawLine(joint_point(joint_by_id[a]), joint_point(joint_by_id[b]))
        for joint in self.joints:
            point = joint_point(joint)
            is_selected = joint["id"] in self.selected
            color = QColor("#ffcf70" if is_selected else "#f1f5f9")
            painter.setPen(QPen(QColor("#151923"), 2.0))
            painter.setBrush(color)
            painter.drawEllipse(point, 6.5, 6.5)
            painter.setPen(QPen(color, 1.0))
            painter.drawText(point + QPointF(10, -8), joint["name"])
        painter.restore()

    def set_mask_layer(self, layer_id: str | None) -> None:
        self.active_layer_id = layer_id
        self._load_active_mask()
        self.viewport().update()

    def set_proposal_preview(self, path: Path | None) -> None:
        self.proposal_overlay = None
        if path is not None:
            try:
                with PILImage.open(path) as source:
                    proposal = source.convert("L")
                tinted = PILImage.new("RGBA", proposal.size, (255, 193, 61, 0))
                tinted.putalpha(proposal.point(lambda value: round(value * 0.45)))
                self.proposal_overlay = QImage(
                    tinted.tobytes(), tinted.width, tinted.height, tinted.width * 4,
                    QImage.Format.Format_RGBA8888,
                ).copy()
            except OSError:
                self.proposal_overlay = None
        self.viewport().update()

    def clear_prompts(self) -> None:
        self.positive_points.clear()
        self.negative_points.clear()
        self.prompt_box = None
        self.temporary_box = None
        self.prompts_changed.emit()
        self.viewport().update()

    def _load_active_mask(self) -> None:
        self.mask_image = None
        self.mask_overlay = None
        self.mask_path = None
        layer = next((item for item in self.layers if item.get("id") == self.active_layer_id), None)
        if layer is None or self.project_dir is None:
            return
        self.mask_path = (self.project_dir / layer["mask"]).resolve()
        try:
            self.mask_image = PILImage.open(self.mask_path).convert("L")
        except OSError:
            return
        tinted = PILImage.new("RGBA", self.mask_image.size, (41, 205, 184, 0))
        tinted.putalpha(self.mask_image.point(lambda value: round(value * 0.42)))
        pixels = tinted.tobytes()
        self.mask_overlay = QImage(
            pixels, tinted.width, tinted.height, tinted.width * 4, QImage.Format.Format_RGBA8888
        ).copy()

    def _paint_mask(self, point: QPointF) -> None:
        if self.mask_image is None or self.mask_path is None:
            return
        x, y = round(point.x()), round(point.y())
        width, height = self.mask_image.size
        x, y = max(0, min(width - 1, x)), max(0, min(height - 1, y))
        color = 0 if self.erase_stroke else 255
        draw = ImageDraw.Draw(self.mask_image)
        radius = max(1, self.brush_size // 2)
        if self.last_paint_point is None:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
        else:
            previous = (round(self.last_paint_point.x()), round(self.last_paint_point.y()))
            draw.line((previous, (x, y)), fill=color, width=self.brush_size)
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
        self.last_paint_point = QPointF(x, y)
        tinted = PILImage.new("RGBA", self.mask_image.size, (41, 205, 184, 0))
        tinted.putalpha(self.mask_image.point(lambda value: round(value * 0.42)))
        self.mask_overlay = QImage(
            tinted.tobytes(), tinted.width, tinted.height, tinted.width * 4,
            QImage.Format.Format_RGBA8888,
        ).copy()
        self.viewport().update()

    def _joint_at(self, point: QPointF) -> str | None:
        radius = 12 / max(0.2, self.transform().m11())
        for joint in reversed(self.joints):
            if math.hypot(joint["x"] - point.x(), joint["y"] - point.y()) <= radius:
                return joint["id"]
        return None

    def mousePressEvent(self, event) -> None:
        point = self.mapToScene(event.position().toPoint())
        if event.button() == Qt.MouseButton.MiddleButton:
            self.panning = True
            self.pan_at = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if self.target_pick_mode and event.button() == Qt.MouseButton.LeftButton and self.image:
            if self.sceneRect().contains(point):
                self.ik_target = (point.x(), point.y())
                self.target_pick_mode = False
                self.ik_target_changed.emit(point.x(), point.y())
                self.viewport().update()
            event.accept()
            return
        if self.prompt_mode and event.button() == Qt.MouseButton.LeftButton and self.image:
            if self.prompt_mode == "box":
                self.box_start = point
                self.temporary_box = (point.x(), point.y(), point.x(), point.y())
                self.drawing_box = True
            elif self.sceneRect().contains(point):
                self.proposal_overlay = None
                target = self.positive_points if self.prompt_mode == "positive" else self.negative_points
                target.append((point.x(), point.y()))
                self.prompts_changed.emit()
                self.viewport().update()
            event.accept()
            return
        if self.paint_mode and self.mask_image is not None and event.button() in (
            Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton
        ):
            self.painting = True
            self.erase_stroke = event.button() == Qt.MouseButton.RightButton
            self.last_paint_point = None
            self._paint_mask(point)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.image:
            if self.inserting:
                if self.sceneRect().contains(point):
                    joint_id = f"joint-{self._sequence}"
                    name = f"Pivote {self._sequence}"
                    self._sequence += 1
                    self.joints.append({"id": joint_id, "name": name, "x": point.x(), "y": point.y(), "parent": None, "rotation": 0.0})
                    self.selected = [joint_id]
                    self.inserting = False
                    self.joint_added.emit(joint_id)
                    self.joint_selected.emit(self.selected)
                    self.changed.emit()
                    self.viewport().update()
                event.accept()
                return
            joint_id = self._joint_at(point)
            if joint_id:
                if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    if joint_id in self.selected:
                        self.selected.remove(joint_id)
                    else:
                        self.selected.append(joint_id)
                else:
                    self.selected = [joint_id]
                    self.dragging = joint_id
                self.joint_selected.emit(self.selected)
                self.viewport().update()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.drawing_box and self.box_start is not None:
            point = self.mapToScene(event.position().toPoint())
            x1, x2 = sorted((self.box_start.x(), point.x()))
            y1, y2 = sorted((self.box_start.y(), point.y()))
            self.temporary_box = (x1, y1, x2, y2)
            self.viewport().update()
            event.accept()
            return
        if self.panning:
            delta = event.position() - self.pan_at
            self.pan_at = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta.y()))
            event.accept()
            return
        if self.painting:
            self._paint_mask(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if self.dragging:
            point = self.mapToScene(event.position().toPoint())
            if self.sceneRect().contains(point):
                joint = next(j for j in self.joints if j["id"] == self.dragging)
                joint["x"], joint["y"] = point.x(), point.y()
                self.changed.emit()
                self.viewport().update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self.panning:
            self.panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        if self.drawing_box and event.button() == Qt.MouseButton.LeftButton:
            self.drawing_box = False
            self.box_start = None
            self.prompt_box = self.temporary_box
            self.temporary_box = None
            if self.prompt_box and self.image:
                x1, y1, x2, y2 = self.prompt_box
                x1, x2 = max(0.0, x1), min(float(self.image.width()), x2)
                y1, y2 = max(0.0, y1), min(float(self.image.height()), y2)
                self.prompt_box = (x1, y1, x2, y2)
                if x2 - x1 < 3 or y2 - y1 < 3:
                    self.prompt_box = None
            self.prompts_changed.emit()
            self.viewport().update()
            event.accept()
            return
        if self.painting and event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self.painting = False
            self.last_paint_point = None
            if self.mask_image is not None and self.active_layer_id:
                self.mask_changed.emit(self.active_layer_id)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.dragging:
            self.dragging = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)


class MainWindow(QMainWindow):
    def __init__(self, library: ProjectLibrary | None = None) -> None:
        super().__init__()
        self.setWindowTitle("RigM · Gestor de rigs 2D")
        self.resize(1180, 760)
        self.project_path: Path | None = None
        self.loaded_mtime_ns: int | None = None
        self.preview_rotations: dict[str, float] = {}
        self.preview_layer_opacities: dict[str, float] = {}
        self.preview_layer_visibility: dict[str, bool] = {}
        self.current_frame = 0
        self.playing = False
        self.playback_direction = 1
        self.playback_clock = QElapsedTimer()
        self.playback_last_elapsed_ms = 0
        self.playback_frame_remainder = 0.0
        self.behavior_runtime: BehaviorRuntime | None = None
        self.behavior_clock = QElapsedTimer()
        self.behavior_last_elapsed_ms = 0
        self.autorig_worker: AvatarAutoRigWorker | None = None
        self.library = library or ProjectLibrary()
        self.canvas = RigCanvas()
        self.canvas.joint_added.connect(self._add_joint_row)
        self.canvas.joint_selected.connect(self._sync_selection)
        self.canvas.changed.connect(self._refresh_status)

        self.joint_list = QListWidget()
        self.joint_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.joint_list.itemSelectionChanged.connect(self._select_from_list)
        self.layer_list = QTreeWidget()
        self.layer_list.setHeaderHidden(True)
        self.layer_list.setIndentation(18)
        self.layer_list.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.layer_list.itemSelectionChanged.connect(self._select_layer)
        self.canvas.mask_changed.connect(self._commit_mask_change)
        self.canvas.prompts_changed.connect(self._update_prompt_status)
        self.canvas.ik_target_changed.connect(self._set_ik_target)
        self.sam2_worker: SegmentationWorker | None = None
        self.decomposition_worker: DecompositionWorker | None = None
        self.sam2_notice_shown = False
        self.status = QLabel("Abre una ilustración para empezar.")
        self.status.setWordWrap(True)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("ESQUELETO"))
        self.open_button = QPushButton("Abrir imagen…")
        self.open_button.clicked.connect(self.open_image)
        layout.addWidget(self.open_button)
        self.add_button = QPushButton("Añadir pivote")
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self.start_adding_joint)
        layout.addWidget(self.add_button)
        self.connect_button = QPushButton("Conectar selección")
        self.connect_button.setEnabled(False)
        self.connect_button.clicked.connect(self.connect_selected)
        layout.addWidget(self.connect_button)
        self.remove_button = QPushButton("Eliminar selección")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self.remove_selected)
        layout.addWidget(self.remove_button)
        layout.addWidget(self.joint_list, 1)
        layout.addWidget(QLabel("CAPAS / MÁSCARAS"))
        layout.addWidget(self.layer_list, 1)
        self.add_layer_button = QPushButton("Crear máscara")
        self.add_layer_button.setEnabled(False)
        self.add_layer_button.clicked.connect(self.create_mask_layer)
        layout.addWidget(self.add_layer_button)
        self.extract_layer_button = QPushButton("Extraer pieza activa")
        self.extract_layer_button.setEnabled(False)
        self.extract_layer_button.clicked.connect(self.extract_active_layer)
        layout.addWidget(self.extract_layer_button)
        self.inpaint_button = QPushButton("Reconstruir zonas ocultas")
        self.inpaint_button.setEnabled(False)
        self.inpaint_button.clicked.connect(self.reconstruct_hidden_areas)
        layout.addWidget(self.inpaint_button)
        self.group_ai_layers_checkbox = QCheckBox("Agrupar capas generadas en una carpeta")
        self.group_ai_layers_checkbox.setChecked(True)
        self.group_ai_layers_checkbox.setToolTip("Muestra las piezas de See-through dentro de un grupo desplegable en el panel de capas.")
        layout.addWidget(self.group_ai_layers_checkbox)
        self.split_ai_parts_checkbox = QCheckBox("Separar izquierda/derecha (experimental)")
        self.split_ai_parts_checkbox.setToolTip(
            "Activa los cortes opcionales de See-through para piezas izquierda/derecha. "
            "No separa por sí sola el torso de los brazos y puede fallar en algunas ilustraciones."
        )
        layout.addWidget(self.split_ai_parts_checkbox)
        self.auto_decompose_button = QPushButton("Separar personaje completo con IA")
        self.auto_decompose_button.setEnabled(False)
        self.auto_decompose_button.setToolTip("Crea capas semánticas de anime y rellena partes ocultas usando See-through en Hugging Face.")
        self.auto_decompose_button.clicked.connect(self._start_ai_decomposition)
        layout.addWidget(self.auto_decompose_button)
        self.import_psd_button = QPushButton("Importar PSD de See-through…")
        self.import_psd_button.setEnabled(False)
        self.import_psd_button.setToolTip("Importa las capas de un PSD generado previamente dentro del proyecto RigM abierto.")
        self.import_psd_button.clicked.connect(self._import_existing_decomposition_psd)
        layout.addWidget(self.import_psd_button)
        layout.addWidget(QLabel("SEGMENTACIÓN SAM 2 · PROMPTS"))
        self.prompt_group = QButtonGroup(self)
        self.prompt_group.setExclusive(True)
        self.positive_prompt_button = QPushButton("+ Punto: conservar")
        self.negative_prompt_button = QPushButton("− Punto: excluir")
        self.box_prompt_button = QPushButton("Marcar caja")
        for button in (self.positive_prompt_button, self.negative_prompt_button, self.box_prompt_button):
            button.setCheckable(True)
            button.setEnabled(False)
            self.prompt_group.addButton(button)
            layout.addWidget(button)
        self.positive_prompt_button.toggled.connect(lambda active: self._set_prompt_mode("positive", active))
        self.negative_prompt_button.toggled.connect(lambda active: self._set_prompt_mode("negative", active))
        self.box_prompt_button.toggled.connect(lambda active: self._set_prompt_mode("box", active))
        self.prompt_status = QLabel("Elige puntos positivos/negativos o una caja.")
        self.prompt_status.setWordWrap(True)
        layout.addWidget(self.prompt_status)
        self.clear_prompts_button = QPushButton("Limpiar indicaciones")
        self.clear_prompts_button.setEnabled(False)
        self.clear_prompts_button.clicked.connect(self._clear_segmentation_prompts)
        layout.addWidget(self.clear_prompts_button)
        self.propose_mask_button = QPushButton("Proponer máscara con SAM 2")
        self.propose_mask_button.setEnabled(False)
        self.propose_mask_button.clicked.connect(self._propose_mask)
        layout.addWidget(self.propose_mask_button)
        self.proposal_combo = QComboBox()
        self.proposal_combo.setEnabled(False)
        self.proposal_combo.currentIndexChanged.connect(self._select_mask_proposal)
        layout.addWidget(self.proposal_combo)
        self.proposal_details = QLabel("SAM puntúa la calidad visual general; revisa que la propuesta corresponda a una sola pieza.")
        self.proposal_details.setWordWrap(True)
        layout.addWidget(self.proposal_details)
        self.accept_proposal_button = QPushButton("Aceptar propuesta")
        self.accept_proposal_button.setEnabled(False)
        self.accept_proposal_button.clicked.connect(self._accept_mask_proposal)
        layout.addWidget(self.accept_proposal_button)
        self.preview_button = QCheckBox("Ver composición reconstruida")
        self.preview_button.setEnabled(False)
        self.preview_button.toggled.connect(self._toggle_composite_preview)
        layout.addWidget(self.preview_button)
        self.paint_button = QPushButton("Pintar máscara")
        self.paint_button.setEnabled(False)
        self.paint_button.setCheckable(True)
        self.paint_button.toggled.connect(self.toggle_mask_paint)
        layout.addWidget(self.paint_button)
        self.brush_slider = QSlider(Qt.Orientation.Horizontal)
        self.brush_slider.setRange(2, 80)
        self.brush_slider.setValue(self.canvas.brush_size)
        self.brush_slider.valueChanged.connect(self._set_brush_size)
        layout.addWidget(QLabel("Tamaño de pincel · clic derecho para borrar"))
        layout.addWidget(self.brush_slider)
        layout.addWidget(QLabel("MALLA Y DEFORMACIÓN"))
        self.mesh_button = QPushButton("Generar malla de la pieza activa")
        self.mesh_button.setEnabled(False)
        self.mesh_button.clicked.connect(self._generate_active_mesh)
        layout.addWidget(self.mesh_button)
        layout.addWidget(QLabel("Rotación de la articulación seleccionada"))
        self.rotation_slider = QSlider(Qt.Orientation.Horizontal)
        self.rotation_slider.setRange(-180, 180)
        self.rotation_slider.setValue(0)
        self.rotation_slider.setEnabled(False)
        self.rotation_slider.valueChanged.connect(self._preview_joint_rotation)
        layout.addWidget(self.rotation_slider)
        self.rotation_value = QLabel("0°")
        layout.addWidget(self.rotation_value)
        layout.addWidget(QLabel("PESOS POR VÉRTICE"))
        self.vertex_spin = QSpinBox()
        self.vertex_spin.setRange(0, 0)
        self.vertex_spin.valueChanged.connect(lambda _value: self._refresh_weight_controls())
        layout.addWidget(self.vertex_spin)
        self.weight_joint_combo = QComboBox()
        self.weight_joint_combo.currentIndexChanged.connect(lambda _index: self._refresh_weight_controls())
        layout.addWidget(self.weight_joint_combo)
        self.weight_slider = QSlider(Qt.Orientation.Horizontal)
        self.weight_slider.setRange(0, 100)
        self.weight_slider.setValue(100)
        layout.addWidget(self.weight_slider)
        self.apply_weight_button = QPushButton("Aplicar peso normalizado")
        self.apply_weight_button.setEnabled(False)
        self.apply_weight_button.clicked.connect(self._apply_vertex_weight)
        layout.addWidget(self.apply_weight_button)
        layout.addWidget(QLabel("IK DE DOS HUESOS"))
        self.ik_pick_button = QPushButton("Marcar objetivo en el lienzo")
        self.ik_pick_button.setCheckable(True)
        self.ik_pick_button.setEnabled(False)
        self.ik_pick_button.toggled.connect(lambda active: setattr(self.canvas, "target_pick_mode", active))
        layout.addWidget(self.ik_pick_button)
        self.ik_status = QLabel("Selecciona raíz, codo/rodilla y extremo con Ctrl.")
        self.ik_status.setWordWrap(True)
        layout.addWidget(self.ik_status)
        self.ik_opposite_bend = QCheckBox("Flexionar al lado opuesto")
        layout.addWidget(self.ik_opposite_bend)
        self.solve_ik_button = QPushButton("Resolver IK de la selección")
        self.solve_ik_button.setEnabled(False)
        self.solve_ik_button.clicked.connect(self._solve_ik_preview)
        layout.addWidget(self.solve_ik_button)
        self.save_ik_button = QPushButton("Guardar IK como claves")
        self.save_ik_button.setEnabled(False)
        self.save_ik_button.clicked.connect(self._save_ik_keys)
        layout.addWidget(self.save_ik_button)
        layout.addWidget(QLabel("ANIMACIÓN"))
        self.avatar3d_section_label = QLabel("IMPORTAR AVATAR 3D")
        layout.addWidget(self.avatar3d_section_label)
        self.avatar3d_attach_button = QPushButton("Importar GLB / VRM…")
        self.avatar3d_attach_button.setEnabled(False)
        self.avatar3d_attach_button.clicked.connect(self._attach_avatar3d)
        layout.addWidget(self.avatar3d_attach_button)
        self.avatar3d_viewer_button = QPushButton("Abrir visor 3D")
        self.avatar3d_viewer_button.setEnabled(False)
        self.avatar3d_viewer_button.setToolTip("Abre el GLB o VRM del proyecto en el visor 3D local.")
        self.avatar3d_viewer_button.clicked.connect(self._open_avatar3d_viewer)
        layout.addWidget(self.avatar3d_viewer_button)
        behavior_row = QHBoxLayout()
        self.behavior_enabled = QCheckBox("Simular agente")
        self.behavior_enabled.setToolTip("Previsualiza estados reactivos en memoria; no cambia el archivo del proyecto.")
        self.behavior_enabled.toggled.connect(self._toggle_behavior_simulation)
        self.behavior_combo = QComboBox()
        for state in BEHAVIOR_STATES:
            self.behavior_combo.addItem({"idle":"Reposo", "listening":"Escuchando", "thinking":"Procesando", "speaking":"Hablando", "waiting":"Esperando", "restless":"Nerviosa · paseando", "error":"Error"}[state], state)
        self.behavior_combo.currentIndexChanged.connect(self._set_behavior_state)
        behavior_row.addWidget(self.behavior_enabled)
        behavior_row.addWidget(self.behavior_combo)
        layout.addLayout(behavior_row)
        self.behavior_intensity = QSlider(Qt.Orientation.Horizontal)
        self.behavior_intensity.setRange(0, 100)
        self.behavior_intensity.setValue(35)
        self.behavior_intensity.valueChanged.connect(self._set_behavior_intensity)
        layout.addWidget(QLabel("Intensidad de movimiento"))
        layout.addWidget(self.behavior_intensity)
        self.clip_combo = QComboBox()
        self.clip_combo.currentIndexChanged.connect(self._select_clip)
        layout.addWidget(self.clip_combo)
        clip_buttons = QHBoxLayout()
        self.create_clip_button = QPushButton("Crear clip")
        self.create_clip_button.clicked.connect(self._create_clip)
        self.play_button = QPushButton("▶ Reproducir")
        self.play_button.setCheckable(True)
        self.play_button.toggled.connect(self._toggle_playback)
        clip_buttons.addWidget(self.create_clip_button)
        clip_buttons.addWidget(self.play_button)
        self.reverse_playback_checkbox = QCheckBox("Inversa")
        self.reverse_playback_checkbox.setToolTip("Reproduce el clip hacia atrás sin cambiar sus claves.")
        self.reverse_playback_checkbox.toggled.connect(self._set_playback_direction)
        clip_buttons.addWidget(self.reverse_playback_checkbox)
        clip_buttons.addWidget(QLabel("Velocidad"))
        self.playback_speed_combo = QComboBox()
        for speed in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0):
            self.playback_speed_combo.addItem(f"{speed:g}×", speed)
        self.playback_speed_combo.setCurrentIndex(self.playback_speed_combo.findData(1.5))
        self.playback_speed_combo.setToolTip(
            "Multiplica los fotogramas por segundo del clip. Si el render va más lento, "
            "se saltan fotogramas intermedios para mantener este ritmo."
        )
        self.playback_speed_combo.currentIndexChanged.connect(self._set_playback_speed)
        clip_buttons.addWidget(self.playback_speed_combo)
        layout.addLayout(clip_buttons)
        self.frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_slider.setRange(0, 60)
        self.frame_slider.valueChanged.connect(self._set_animation_frame)
        layout.addWidget(self.frame_slider)
        frame_buttons = QHBoxLayout()
        self.frame_start_button = QPushButton("⏮ Inicio")
        self.frame_start_button.clicked.connect(lambda: self._go_to_frame_edge(False))
        self.frame_back_button = QPushButton("◀ 1 fotograma")
        self.frame_back_button.clicked.connect(lambda: self._step_animation_frame(-1))
        self.frame_forward_button = QPushButton("1 fotograma ▶")
        self.frame_forward_button.clicked.connect(lambda: self._step_animation_frame(1))
        self.frame_end_button = QPushButton("Fin ⏭")
        self.frame_end_button.clicked.connect(lambda: self._go_to_frame_edge(True))
        for button in (self.frame_start_button, self.frame_back_button,
                       self.frame_forward_button, self.frame_end_button):
            frame_buttons.addWidget(button)
        layout.addLayout(frame_buttons)
        self.frame_label = QLabel("Fotograma 0")
        layout.addWidget(self.frame_label)
        interpolation_row = QHBoxLayout()
        interpolation_row.addWidget(QLabel("Curva al guardar claves"))
        self.interpolation_combo = QComboBox()
        self.interpolation_combo.addItem("Cúbica monótona", "cubic")
        self.interpolation_combo.addItem("Suave por tramo", "smooth")
        self.interpolation_combo.addItem("Lineal", "linear")
        self.interpolation_combo.setToolTip("La cúbica conserva la velocidad entre claves y evita frenadas en cada una.")
        interpolation_row.addWidget(self.interpolation_combo)
        layout.addLayout(interpolation_row)
        self.key_button = QPushButton("Guardar clave de rotación")
        self.key_button.setEnabled(False)
        self.key_button.clicked.connect(self._save_rotation_key)
        layout.addWidget(self.key_button)
        self.layer_visibility_checkbox = QCheckBox("Capa seleccionada visible en este fotograma")
        self.layer_visibility_checkbox.toggled.connect(self._preview_layer_visibility)
        layout.addWidget(self.layer_visibility_checkbox)
        self.layer_visibility_key_button = QPushButton("Guardar clave de visibilidad")
        self.layer_visibility_key_button.setEnabled(False)
        self.layer_visibility_key_button.clicked.connect(self._save_layer_visibility_key)
        layout.addWidget(self.layer_visibility_key_button)
        layout.addWidget(QLabel("EXPRESIONES / CARA"))
        self.expression_combo = QComboBox()
        self.expression_combo.currentIndexChanged.connect(self._preview_expression)
        layout.addWidget(self.expression_combo)
        self.create_expression_button = QPushButton("Guardar expresión desde la pose")
        self.create_expression_button.clicked.connect(self._create_expression_from_pose)
        layout.addWidget(self.create_expression_button)
        layout.addWidget(QLabel("Opacidad de la capa seleccionada"))
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.valueChanged.connect(self._preview_layer_opacity)
        layout.addWidget(self.opacity_slider)
        self.expression_value_button = QPushButton("Guardar opacidad en expresión")
        self.expression_value_button.clicked.connect(self._save_expression_opacity)
        layout.addWidget(self.expression_value_button)
        self.save_button = QPushButton("Guardar proyecto…")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_project)
        layout.addWidget(self.save_button)
        self.export_button = QPushButton("Exportar bundle portable…")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_current_project)
        layout.addWidget(self.export_button)
        self.reload_button = QPushButton("Recargar cambios externos")
        self.reload_button.setEnabled(False)
        self.reload_button.clicked.connect(self.reload_current_project)
        layout.addWidget(self.reload_button)
        self.status.setStyleSheet("color: #aab4c5; padding: 8px 2px;")
        layout.addWidget(self.status)
        layout.addWidget(QLabel("Rueda: zoom · botón central: mover lienzo"))
        panel.setMinimumWidth(245)
        panel.setMaximumWidth(300)

        central = QWidget()
        row = QHBoxLayout(central)
        panel_scroll = QScrollArea()
        panel_scroll.setWidgetResizable(True)
        panel_scroll.setWidget(panel)
        panel_scroll.setMinimumWidth(260)
        panel_scroll.setMaximumWidth(330)
        self.tool_tabs = QTabWidget()
        self.tool_tabs.setMinimumWidth(260)
        self.tool_tabs.setMaximumWidth(330)
        self.tool_tabs.addTab(panel_scroll, "2D · Ilustración")

        # Move the shared avatar controls to their own 3D workspace. All existing
        # image segmentation, masks, 2D rigging, clips and expressions stay together.
        layout.removeWidget(self.avatar3d_section_label)
        layout.removeWidget(self.avatar3d_attach_button)
        layout.removeWidget(self.avatar3d_viewer_button)
        avatar_panel = QWidget()
        avatar_layout = QVBoxLayout(avatar_panel)
        avatar_layout.addWidget(QLabel("AVATAR 3D · GLB / VRM"))
        avatar_intro = QLabel(
            "Puedes importar un GLB con o sin esqueleto. RigM propone un esqueleto y pesos desde la geometría "
            "sin Blender; revisa el resultado porque la inferencia es aproximada."
        )
        avatar_intro.setWordWrap(True)
        avatar_layout.addWidget(avatar_intro)
        avatar_layout.addWidget(self.avatar3d_section_label)
        avatar_layout.addWidget(self.avatar3d_attach_button)
        avatar_layout.addWidget(QLabel("Frente del personaje (para el análisis)"))
        self.autorig_forward_combo = QComboBox()
        for axis, label in (("+Z", "+Z · GLB habitual"), ("-Z", "−Z"), ("+X", "+X"), ("-X", "−X")):
            self.autorig_forward_combo.addItem(label, axis)
        self.autorig_forward_combo.setToolTip("Dirección frontal del modelo glTF. El auto-rig geométrico actual infiere una pose humanoide erguida.")
        avatar_layout.addWidget(self.autorig_forward_combo)
        self.avatar3d_autorig_button = QPushButton("Analizar modelo y crear esqueleto")
        self.avatar3d_autorig_button.setEnabled(False)
        self.avatar3d_autorig_button.setToolTip("Propone huesos y pesos geométricos dentro del visor 3D. Conserva el GLB fuente.")
        self.avatar3d_autorig_button.clicked.connect(self._auto_rig_avatar3d)
        avatar_layout.addWidget(self.avatar3d_autorig_button)
        self.avatar3d_rebuild_button = QPushButton("Rehacer esqueleto desde fuente")
        self.avatar3d_rebuild_button.setEnabled(False)
        self.avatar3d_rebuild_button.setToolTip("Vuelve al GLB original conservado en el proyecto y genera un rig nuevo.")
        self.avatar3d_rebuild_button.clicked.connect(self._rebuild_avatar3d_from_source)
        avatar_layout.addWidget(self.avatar3d_rebuild_button)
        avatar_layout.addWidget(self.avatar3d_viewer_button)
        self.avatar3d_info = QLabel("La inferencia asume un humanoide erguido. El rig generado es editable y requiere revisión de pivotes/pesos.")
        self.avatar3d_info.setWordWrap(True)
        avatar_layout.addWidget(self.avatar3d_info)
        avatar_layout.addStretch(1)
        avatar_scroll = QScrollArea()
        avatar_scroll.setWidgetResizable(True)
        avatar_scroll.setMinimumWidth(260)
        avatar_scroll.setMaximumWidth(330)
        avatar_scroll.setWidget(avatar_panel)
        self.tool_tabs.addTab(avatar_scroll, "3D · Avatar")
        self.preview_tabs = QTabWidget()
        self.preview_tabs.setMinimumWidth(280)
        self.preview_tabs.addTab(self.canvas, "Vista 2D")
        self.avatar3d_placeholder = QLabel("Importa un GLB/VRM y abre la vista 3D para previsualizarlo.")
        self.avatar3d_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_tabs.addTab(self.avatar3d_placeholder, "Vista 3D")
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.addWidget(self.tool_tabs)
        self.workspace_splitter.addWidget(self.preview_tabs)
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setSizes([300, 880])
        row.addWidget(self.workspace_splitter, 1)

        self.library_table = QTableWidget(0, 6)
        self.library_table.setHorizontalHeaderLabels(["Rig", "Estado", "Etapa", "Capas", "Progreso", "Actualizado"])
        self.library_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4, 5):
            self.library_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.library_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.library_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.library_table.cellDoubleClicked.connect(lambda *_: self.open_selected_project())

        library_page = QWidget()
        library_layout = QVBoxLayout(library_page)
        library_layout.addWidget(QLabel("BIBLIOTECA DE RIGS"))
        library_layout.addWidget(QLabel("Proyectos registrados y comprobados en disco."))
        library_layout.addWidget(self.library_table, 1)
        library_buttons = QHBoxLayout()
        new_project_button = QPushButton("Crear rig desde imagen…")
        new_project_button.clicked.connect(self.create_new_project)
        import_project_button = QPushButton("Registrar proyecto existente…")
        import_project_button.clicked.connect(self.open_project)
        open_selected_button = QPushButton("Abrir seleccionado")
        open_selected_button.clicked.connect(self.open_selected_project)
        validate_button = QPushButton("Validar")
        validate_button.clicked.connect(self.validate_selected_project)
        remove_button = QPushButton("Quitar de biblioteca")
        remove_button.clicked.connect(self.remove_selected_from_library)
        refresh_button = QPushButton("Actualizar")
        refresh_button.clicked.connect(self.refresh_library)
        library_buttons.addWidget(new_project_button)
        library_buttons.addWidget(import_project_button)
        library_buttons.addWidget(open_selected_button)
        library_buttons.addWidget(validate_button)
        library_buttons.addWidget(remove_button)
        library_buttons.addWidget(refresh_button)
        library_layout.addLayout(library_buttons)

        self.tabs = QTabWidget()
        self.tabs.addTab(library_page, "Biblioteca")
        self.tabs.addTab(central, "Editor")
        self.setCentralWidget(self.tabs)
        self._make_menu()
        self.refresh_library()
        self.library_timer = QTimer(self)
        self.library_timer.setInterval(2500)
        self.library_timer.timeout.connect(self.refresh_library)
        self.library_timer.start()
        self.playback_timer = QTimer(self)
        self.playback_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.playback_timer.timeout.connect(self._advance_animation_frame)
        self.behavior_timer = QTimer(self)
        self.behavior_timer.setInterval(33)
        self.behavior_timer.timeout.connect(self._advance_behavior_simulation)

    def _make_menu(self) -> None:
        file_menu = self.menuBar().addMenu("Archivo")
        open_action = QAction("Abrir imagen…", self)
        open_action.triggered.connect(self.open_image)
        file_menu.addAction(open_action)
        load_action = QAction("Abrir proyecto…", self)
        load_action.triggered.connect(self.open_project)
        file_menu.addAction(load_action)
        save_action = QAction("Guardar proyecto…", self)
        save_action.triggered.connect(self.save_project)
        file_menu.addAction(save_action)

    def open_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Abrir ilustración", "", IMAGE_FILTER)
        if not path:
            return
        try:
            if self.behavior_runtime:
                self.behavior_enabled.setChecked(False)
            self.canvas.set_image(Path(path))
            self._reset_prompt_tools()
            self.project_path = None
            self.loaded_mtime_ns = None
            self.avatar3d_attach_button.setEnabled(False)
            self.avatar3d_viewer_button.setEnabled(False)
            self.avatar3d_autorig_button.setEnabled(False)
            self.reload_button.setEnabled(False)
            self.joint_list.clear()
            self.paint_button.setChecked(False)
            self._refresh_layer_list()
            self._set_ready(True)
            self.status.setText(f"Imagen: {Path(path).name}. Añade pivotes para armar el rig.")
            self.tabs.setCurrentIndex(1)
        except ValueError as exc:
            QMessageBox.warning(self, "No pude abrir la imagen", str(exc))

    def create_new_project(self) -> None:
        image, _ = QFileDialog.getOpenFileName(self, "Elegir imagen del personaje", "", IMAGE_FILTER)
        if not image:
            return
        image_path = Path(image)
        default_dir = Path.home() / "RigM" / "Projects"
        default_path = default_dir / f"{image_path.stem}.rigm"
        output, _ = QFileDialog.getSaveFileName(self, "Crear proyecto RigM", str(default_path), PROJECT_FILTER)
        if not output:
            return
        project_path = Path(output)
        if project_path.suffix.lower() != ".rigm":
            project_path = project_path.with_suffix(".rigm")
        try:
            create_project(image_path, project_path, image_path.stem)
            self.library.add(project_path)
            self._open_project_file(project_path)
            self.refresh_library()
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude crear el rig", str(exc))

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Abrir proyecto RigM", "", PROJECT_FILTER)
        if not path:
            return
        try:
            project_path = Path(path).resolve()
            self.library.add(project_path)
            self._open_project_file(project_path)
            self.refresh_library()
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude abrir el proyecto", str(exc))

    def _open_project_file(self, project_path: Path) -> None:
        if self.behavior_runtime:
            self.behavior_enabled.setChecked(False)
        data = load_project(project_path)
        self.canvas.load_model(data, project_path)
        self._reset_prompt_tools()
        self.preview_rotations.clear()
        self.preview_layer_opacities.clear()
        self.preview_layer_visibility.clear()
        self.project_path = project_path.resolve()
        self.avatar3d_attach_button.setEnabled(True)
        avatar = data.get("avatar3d", {})
        if not isinstance(avatar, dict):
            avatar = {}
        self.avatar3d_viewer_button.setEnabled(bool(avatar))
        self.avatar3d_autorig_button.setEnabled(bool(avatar and avatar.get("format") == "glb" and not avatar.get("rigged")))
        source_model = avatar.get("source_path")
        self.avatar3d_rebuild_button.setEnabled(bool(
            avatar.get("format") == "glb" and avatar.get("rigged") and source_model
            and (self.project_path.parent / source_model).is_file()
        ))
        self.avatar3d_info.setText(
            "GLB con esqueleto" if avatar.get("format") == "glb" and avatar.get("rigged")
            else "GLB sin esqueleto: puedes previsualizarlo o crear un auto-rig." if avatar.get("format") == "glb"
            else "Avatar VRM con esqueleto humanoide." if avatar.get("format") == "vrm"
            else "Importa un GLB o VRM. RigM puede proponer huesos para un GLB sin esqueleto."
        )
        self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
        self.reload_button.setEnabled(False)
        self.joint_list.clear()
        for joint in self.canvas.joints:
            self._add_joint_row(joint["id"])
        self._refresh_layer_list()
        self._refresh_motion_controls(data)
        self._refresh_composite_preview(show_error=False)
        self._set_ready(True)
        self.status.setText(f"Proyecto: {data.get('name', project_path.stem)} · {data.get('stage', 'skeleton')}")
        self.tabs.setCurrentIndex(1)

    def _attach_avatar3d(self) -> None:
        if self.project_path is None:
            return
        if self.loaded_mtime_ns is not None and self.project_path.stat().st_mtime_ns != self.loaded_mtime_ns:
            QMessageBox.warning(self, "Cambios externos", "El proyecto cambió desde que lo abriste. Recárgalo antes de importar un avatar.")
            self.reload_button.setEnabled(True)
            return
        path, _ = QFileDialog.getOpenFileName(self, "Importar modelo 3D", "", "Modelos 3D (*.glb *.vrm)")
        if not path:
            return
        if self.loaded_mtime_ns is not None and self.project_path.stat().st_mtime_ns != self.loaded_mtime_ns:
            QMessageBox.warning(self, "Cambios externos", "El proyecto cambió mientras elegías el modelo. Recárgalo antes de importar.")
            self.reload_button.setEnabled(True)
            return
        try:
            result = attach_avatar(self.project_path, path, actor="user")
            self._open_project_file(self.project_path)
            self.refresh_library()
            warnings = result["validation"]["warnings"]
            detail = "El modelo GLB está riggeado." if result["avatar"]["rigged"] else "El GLB se importó para análisis; no trae esqueleto todavía."
            if result["avatar"]["format"] == "vrm":
                detail = "El avatar VRM pasó la validación humanoide."
            if warnings:
                detail += " Revisa: " + " ".join(warnings)
            self.status.setText(detail)
            if result["avatar"]["format"] == "glb":
                self.tool_tabs.setCurrentIndex(1)
            self._open_avatar3d_viewer()
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude importar el modelo 3D", str(exc))

    def _auto_rig_avatar3d(self) -> None:
        if self.project_path is None:
            return
        if self.loaded_mtime_ns is not None and self.project_path.stat().st_mtime_ns != self.loaded_mtime_ns:
            QMessageBox.warning(self, "Cambios externos", "El proyecto cambió desde que lo abriste. Recárgalo antes de crear el esqueleto.")
            self.reload_button.setEnabled(True)
            return
        avatar = load_project(self.project_path).get("avatar3d", {})
        if avatar.get("format") != "glb" or avatar.get("rigged"):
            QMessageBox.information(self, "Auto-rig 3D", "Importa primero un GLB sin esqueleto.")
            return
        self._open_avatar3d_viewer()
        dialog = getattr(self, "avatar3d_dialog", None)
        if dialog and dialog.allow_autorig:
            dialog.request_autorig()

    def _choose_blender_binary(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Seleccionar ejecutable de Blender", "", "Todos los archivos (*)")
        if path:
            self.blender_path_edit.setText(path)

    def _avatar_autorig_succeeded(self, result: dict[str, Any]) -> None:
        self._open_project_file(self.project_path)
        self.refresh_library()
        analysis = result.get("analysis", {})
        confidence = analysis.get("confidence", "low")
        self.avatar3d_info.setText(f"Esqueleto creado · confianza geométrica {confidence}. Revisa huesos y pesos en Blender antes de animar.")
        self.status.setText("Auto-rig 3D guardado. Se conservó el GLB original y el nuevo rig quedó dentro del proyecto.")
        if analysis.get("warnings"):
            QMessageBox.information(self, "Revisa el auto-rig", "\n".join(analysis["warnings"]))

    def _avatar_autorig_failed(self, message: str) -> None:
        self.avatar3d_attach_button.setEnabled(True)
        self.avatar3d_viewer_button.setEnabled(True)
        self.avatar3d_autorig_button.setEnabled(True)
        self.avatar3d_info.setText(message)
        QMessageBox.warning(self, "No pude crear el esqueleto", message)

    def _avatar_autorig_finished(self) -> None:
        self.autorig_worker = None

    def _open_avatar3d_viewer(self, *, model_path_override: Path | None = None, allow_autorig_override: bool | None = None) -> None:
        if self.project_path is None:
            return
        data = load_project(self.project_path)
        result = validate_project_avatar(data, self.project_path)
        if not result:
            QMessageBox.information(self, "Avatar 3D", "Este proyecto aún no tiene un avatar GLB o VRM.")
            return
        if result["errors"]:
            QMessageBox.warning(self, "Avatar 3D no válido", "\n".join(result["errors"]))
            return
        avatar = data["avatar3d"]
        model_path = model_path_override or (self.project_path.parent / avatar["path"])
        allow_autorig = avatar.get("format") == "glb" and not avatar.get("rigged")
        if allow_autorig_override is not None:
            allow_autorig = allow_autorig_override
        try:
            previous = getattr(self, "avatar3d_dialog", None)
            if previous:
                index = self.preview_tabs.indexOf(previous)
                if index >= 0:
                    self.preview_tabs.removeTab(index)
                previous.deleteLater()
            self.avatar3d_dialog = Avatar3DDialog(
                model_path, avatar.get("name", "Avatar 3D"), self,
                project_path=self.project_path, expected_mtime_ns=self.loaded_mtime_ns,
                allow_autorig=allow_autorig,
                forward_axis=self.autorig_forward_combo.currentData(),
            )
            self.avatar3d_dialog.setWindowFlags(Qt.WindowType.Widget)
            self.avatar3d_dialog.setMinimumSize(0, 0)
            self.avatar3d_dialog.autorig_saved.connect(self._embedded_autorig_saved)
            self.avatar3d_dialog.autorig_failed.connect(lambda message: self.avatar3d_info.setText(message))
            index = self.preview_tabs.addTab(self.avatar3d_dialog, "Vista 3D · Avatar")
            self.preview_tabs.setCurrentIndex(index)
            if result["warnings"]:
                self.avatar3d_dialog.message.setText("Antes de evaluar la actuación, revisa estos avisos: " + " ".join(result["warnings"]))
        except ProjectError as exc:
            QMessageBox.information(self, "Instala el visor 3D", str(exc))

    def _rebuild_avatar3d_from_source(self) -> None:
        if self.project_path is None:
            return
        if self.loaded_mtime_ns is not None and self.project_path.stat().st_mtime_ns != self.loaded_mtime_ns:
            QMessageBox.warning(self, "Cambios externos", "El proyecto cambió desde que lo abriste. Recárgalo antes de rehacer el esqueleto.")
            self.reload_button.setEnabled(True)
            return
        avatar = load_project(self.project_path).get("avatar3d", {})
        source = avatar.get("source_path") if isinstance(avatar, dict) else None
        if not source:
            QMessageBox.information(self, "Fuente no disponible", "Este avatar no conserva una ruta de fuente para reconstruir el auto-rig.")
            return
        source_path = (self.project_path.parent / source).resolve()
        if not source_path.is_relative_to(self.project_path.parent.resolve()) or not source_path.is_file():
            QMessageBox.warning(self, "Fuente no disponible", "No encuentro el GLB fuente conservado dentro del proyecto.")
            return
        self._open_avatar3d_viewer(model_path_override=source_path, allow_autorig_override=True)

    def _embedded_autorig_saved(self, _payload: dict[str, Any]) -> None:
        if self.project_path is None:
            return
        self._open_project_file(self.project_path)
        self.refresh_library()
        self.avatar3d_info.setText("GLB actualizado. RigM conserva las versiones fuente; revisa la pose neutral, pivotes y deformación.")
        self.status.setText("Avatar 3D actualizado en el proyecto.")
        self._open_avatar3d_viewer()

    def open_selected_project(self) -> None:
        row = self.library_table.currentRow()
        if row < 0:
            return
        path_item = self.library_table.item(row, 0)
        project_path = Path(path_item.data(Qt.ItemDataRole.UserRole))
        try:
            self._open_project_file(project_path)
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude abrir el proyecto", str(exc))

    def refresh_library(self) -> None:
        selected_path = None
        if self.library_table.currentRow() >= 0:
            selected_path = self.library_table.item(self.library_table.currentRow(), 0).data(Qt.ItemDataRole.UserRole)
        entries = self.library.list_projects()
        labels = {
            "in_progress": "En curso", "queued": "En cola", "needs_review": "Revisión",
            "ready": "Listo", "failed": "Falló", "missing": "Falta archivo",
            "missing_source": "Falta imagen", "invalid": "Archivo inválido",
        }
        self.library_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            updated_at = str(entry.get("updated_at") or "")
            columns = [entry["name"], labels.get(entry["status"], entry["status"]), entry["stage"], str(entry.get("layer_count", 0)), f"{entry.get('progress', 0)}%", updated_at[:19].replace("T", " ")]
            for column, value in enumerate(columns):
                item = QTableWidgetItem(value or "—")
                item.setData(Qt.ItemDataRole.UserRole, entry["path"])
                tooltip = entry["path"]
                if entry.get("task_id"):
                    tooltip += f"\nTarea: {entry['task_id']}"
                if entry.get("last_result"):
                    tooltip += f"\n{entry['last_result']}"
                item.setToolTip(tooltip)
                self.library_table.setItem(row, column, item)
            if entry["path"] == selected_path:
                self.library_table.setCurrentCell(row, 0)
        if self.project_path and self.project_path.exists() and self.loaded_mtime_ns is not None:
            changed_externally = self.project_path.stat().st_mtime_ns != self.loaded_mtime_ns
            self.reload_button.setEnabled(changed_externally)
            if changed_externally:
                self.canvas.paint_mode = False
                self.paint_button.setChecked(False)
                self.paint_button.setEnabled(False)
                self.add_layer_button.setEnabled(False)
                self.extract_layer_button.setEnabled(False)
                self.inpaint_button.setEnabled(False)
                self.status.setText("Otro proceso modificó este proyecto. Recárgalo antes de seguir guardando.")

    def validate_selected_project(self) -> None:
        row = self.library_table.currentRow()
        if row < 0:
            return
        path = self.library_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        try:
            result = validate_project(path)
            lines = [f"Estado: {result['status']}", f"Pivotes: {result['joint_count']} · Huesos: {result['bone_count']}"]
            lines.extend(f"Error: {message}" for message in result["errors"])
            lines.extend(f"Pendiente: {message}" for message in result["warnings"])
            QMessageBox.information(self, "Validación de RigM", "\n".join(lines))
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude validar", str(exc))

    def remove_selected_from_library(self) -> None:
        row = self.library_table.currentRow()
        if row < 0:
            return
        path = self.library_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self.library.remove(path)
        self.refresh_library()

    def _selected_layer_id(self) -> str | None:
        item = self.layer_list.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _refresh_layer_list(self) -> None:
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        group_items: dict[str, QTreeWidgetItem] = {}
        for layer in self.canvas.layers:
            layer_id = layer.get("id")
            group_id = layer.get("group_id")
            if group_id:
                group_item = group_items.get(group_id)
                if group_item is None:
                    group_item = QTreeWidgetItem([layer.get("group_name", "Capas agrupadas")])
                    group_item.setData(0, Qt.ItemDataRole.UserRole, None)
                    font = group_item.font(0)
                    font.setBold(True)
                    group_item.setFont(0, font)
                    self.layer_list.addTopLevelItem(group_item)
                    group_items[group_id] = group_item
                item = QTreeWidgetItem([layer.get("name", layer_id)])
                item.setData(0, Qt.ItemDataRole.UserRole, layer_id)
                group_item.addChild(item)
            else:
                item = QTreeWidgetItem([layer.get("name", layer_id)])
                item.setData(0, Qt.ItemDataRole.UserRole, layer_id)
                self.layer_list.addTopLevelItem(item)
        for group_item in group_items.values():
            group_item.setExpanded(True)
        if self.canvas.active_layer_id:
            for index in range(self.layer_list.topLevelItemCount()):
                top_item = self.layer_list.topLevelItem(index)
                candidates = [top_item] + [top_item.child(child_index) for child_index in range(top_item.childCount())]
                for item in candidates:
                    if item.data(0, Qt.ItemDataRole.UserRole) == self.canvas.active_layer_id:
                        self.layer_list.setCurrentItem(item)
                        break
        self.layer_list.blockSignals(False)
        has_layers = bool(self.canvas.layers)
        can_edit = self.project_path is not None and not self._project_has_external_changes()
        selected_layer = self._selected_layer_id() is not None
        self.add_layer_button.setEnabled(can_edit)
        self.paint_button.setEnabled(has_layers and can_edit)
        for button in (self.positive_prompt_button, self.negative_prompt_button, self.box_prompt_button):
            button.setEnabled(can_edit and selected_layer)
        self.clear_prompts_button.setEnabled(can_edit and selected_layer)
        self.propose_mask_button.setEnabled(can_edit and selected_layer and self.sam2_worker is None)
        self.extract_layer_button.setEnabled(can_edit and selected_layer)
        self.inpaint_button.setEnabled(can_edit and any(
            layer.get("mask") for layer in self.canvas.layers
        ))
        self.auto_decompose_button.setEnabled(can_edit and self.project_path is not None
                                              and self.decomposition_worker is None and self.sam2_worker is None)
        self.import_psd_button.setEnabled(can_edit and self.project_path is not None)
        self.paint_button.setChecked(False)
        self.canvas.paint_mode = False
        self._refresh_proposals_for_current_layer()
        self.mesh_button.setEnabled(can_edit and selected_layer)
        self.create_clip_button.setEnabled(can_edit)
        self.create_expression_button.setEnabled(can_edit)
        self.opacity_slider.setEnabled(can_edit and selected_layer)
        self.expression_value_button.setEnabled(can_edit and selected_layer and self.expression_combo.count() > 0)

    def create_mask_layer(self) -> None:
        if self.project_path is None:
            QMessageBox.information(self, "Guarda el proyecto", "Guarda primero el proyecto para crear una máscara dentro de su carpeta.")
            self.save_project()
            if self.project_path is None or self.loaded_mtime_ns != self.project_path.stat().st_mtime_ns:
                return
        name, accepted = QInputDialog.getText(self, "Crear máscara", "Nombre de la pieza (por ejemplo, brazo_derecho):")
        if not accepted or not name.strip():
            return
        try:
            # Persiste los cambios del esqueleto antes de modificar el manifiesto.
            self.save_project()
            if self.loaded_mtime_ns != self.project_path.stat().st_mtime_ns:
                return
            create_layer(self.project_path, name.strip(), actor="user")
            self._open_project_file(self.project_path)
            self._refresh_layer_list()
            self.refresh_library()
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude crear la máscara", str(exc))

    def _select_layer(self) -> None:
        layer_id = self._selected_layer_id()
        self.canvas.set_mask_layer(layer_id)
        can_edit = not self._project_has_external_changes()
        self.paint_button.setEnabled(layer_id is not None and can_edit)
        self.extract_layer_button.setEnabled(self.project_path is not None and layer_id is not None and can_edit)
        if self.paint_button.isChecked():
            self.paint_button.setChecked(False)
        self._refresh_weight_controls()
        self._refresh_layer_visibility_control()
        self._preview_expression(self.expression_combo.currentIndex())
        self._refresh_proposals_for_current_layer()

    def _set_prompt_mode(self, mode: str, active: bool) -> None:
        if active and self.paint_button.isChecked():
            self.paint_button.setChecked(False)
        self.canvas.prompt_mode = mode if active else None
        self.canvas.viewport().update()

    def _update_prompt_status(self) -> None:
        positive = len(self.canvas.positive_points)
        negative = len(self.canvas.negative_points)
        box = " · caja marcada" if self.canvas.prompt_box else ""
        self.prompt_status.setText(f"Puntos para conservar: {positive} · excluir: {negative}{box}")

    def _clear_segmentation_prompts(self) -> None:
        self.canvas.clear_prompts()
        self.canvas.set_proposal_preview(None)
        self.proposal_combo.setCurrentIndex(-1)

    def _reset_prompt_tools(self) -> None:
        self.canvas.clear_prompts()
        self.prompt_group.setExclusive(False)
        for button in (self.positive_prompt_button, self.negative_prompt_button, self.box_prompt_button):
            button.setChecked(False)
        self.prompt_group.setExclusive(True)
        self.canvas.prompt_mode = None

    def _propose_mask(self) -> None:
        if self.project_path is None or self.canvas.active_layer_id is None or self.sam2_worker is not None:
            return
        if not self.canvas.positive_points and self.canvas.prompt_box is None:
            QMessageBox.information(self, "Falta una indicación", "Añade un punto positivo o dibuja una caja alrededor de la pieza.")
            return
        if self._project_has_external_changes():
            QMessageBox.warning(self, "Cambios externos", "Recarga el proyecto antes de crear una propuesta.")
            return
        if not self.sam2_notice_shown:
            answer = QMessageBox.question(
                self,
                "Segmentación local con SAM 2",
                "SAM 2 procesa la ilustración localmente. Si el modelo aún no está instalado, RigM descargará sus pesos oficiales de Hugging Face una vez. La imagen no se envía a ningún servicio. ¿Continuar?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.sam2_notice_shown = True
        self.canvas.prompt_mode = None
        worker = SegmentationWorker(
            self.project_path,
            self.canvas.active_layer_id,
            list(self.canvas.positive_points),
            list(self.canvas.negative_points),
            self.canvas.prompt_box,
        )
        worker.succeeded.connect(self._sam2_proposals_ready)
        worker.failed.connect(self._sam2_failed)
        worker.finished.connect(self._sam2_worker_finished)
        self.sam2_worker = worker
        self.propose_mask_button.setEnabled(False)
        self.accept_proposal_button.setEnabled(False)
        self.status.setText("SAM 2 está proponiendo máscaras. En CPU puede tardar un poco.")
        worker.start()

    def _import_existing_decomposition_psd(self) -> None:
        if self.project_path is None or self._project_has_external_changes():
            QMessageBox.information(
                self, "Abre un proyecto RigM",
                "Primero abre o crea un proyecto RigM y recarga si tiene cambios externos.",
            )
            return
        psd_path, _ = QFileDialog.getOpenFileName(
            self, "Importar PSD de See-through", str(Path.home() / "Downloads"),
            "Photoshop (*.psd)",
        )
        if not psd_path:
            return
        project_path = self.project_path.resolve()
        data = load_project(project_path)
        existing = any(layer.get("decomposition", {}).get("engine") == "see-through"
                       for layer in data.get("layers", []))
        if existing:
            answer = QMessageBox.question(
                self, "Reemplazar capas de See-through",
                "Este proyecto ya tiene capas importadas de See-through. La importación las reemplazará. ¿Continuar?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            with PILImage.open(data["_image_path"]) as source_image:
                width, height = source_image.size
            side = max(width, height)
            offset = ((side - width) // 2, (side - height) // 2)
            psd_source = Path(psd_path).resolve()
            stored_psd = project_path.parent / "assets" / "decompositions" / f"see-through-{uuid.uuid4().hex[:12]}.psd"
            stored_psd.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(psd_source, stored_psd)
            try:
                count = import_decomposition_psd(
                    project_path, stored_psd, (side, side), (width, height), offset,
                    group_layers=self.group_ai_layers_checkbox.isChecked(),
                )
            except Exception:
                stored_psd.unlink(missing_ok=True)
                raise
            self.loaded_mtime_ns = project_path.stat().st_mtime_ns
            self._open_project_file(project_path)
            imported = load_project(project_path).get("decomposition", {})
            warnings = imported.get("quality", {}).get("warnings", [])
            self.status.setText(f"PSD importado: {count} capas de See-through. Revisa máscaras, orden y reconstrucciones antes de animar.")
            if warnings:
                QMessageBox.warning(
                    self, "Revisión necesaria de la separación",
                    "La importación detectó señales de posible error:\n\n" + "\n".join(f"• {item}" for item in warnings),
                )
            self.refresh_library()
        except (OSError, ProjectError, ValueError) as exc:
            QMessageBox.warning(self, "No pude importar el PSD", str(exc))

    def _start_ai_decomposition(self) -> None:
        if self.project_path is None or self._project_has_external_changes():
            QMessageBox.information(self, "Guarda o recarga el proyecto", "La separación automática necesita un proyecto RigM guardado y sin cambios externos.")
            return
        try:
            from huggingface_hub import get_token
            token = get_token()
        except ImportError:
            token = None
        if not token:
            QMessageBox.information(
                self, "Inicia sesión en Hugging Face",
                "See-through requiere una cuenta y un token de Hugging Face. En una terminal ejecuta:\n\n"
                "hf auth login\n\n"
                "Después vuelve a abrir RigM.",
            )
            return
        answer = QMessageBox.question(
            self, "Separación completa con IA",
            "RigM enviará una copia cuadrada de la ilustración a See-through en Hugging Face. "
            "El servicio puede tardar unos minutos y devuelve un PSD con capas semánticas e inpainting. "
            "La imagen original del proyecto no se modifica. ¿Enviar esta imagen al servicio?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.auto_decompose_button.setEnabled(False)
        self.status.setText("See-through está separando las partes y reconstruyendo zonas ocultas; puede tardar unos minutos.")
        worker = DecompositionWorker(
            self.project_path, token, group_layers=self.group_ai_layers_checkbox.isChecked(),
            split_left_right=self.split_ai_parts_checkbox.isChecked(),
        )
        worker.succeeded.connect(self._decomposition_ready)
        worker.failed.connect(self._decomposition_failed)
        worker.finished.connect(self._decomposition_finished)
        self.decomposition_worker = worker
        worker.start()

    def _decomposition_ready(self, result: dict[str, Any]) -> None:
        if self.project_path is None:
            return
        try:
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self._open_project_file(self.project_path)
            warnings = result.get("quality_warnings", [])
            self.status.setText(
                f"Separación terminada: {result['layer_count']} capas importadas. "
                f"{('Agrupadas en «' + result['group_name'] + '». ') if result.get('group_name') else ''}"
                "Revisa máscaras, orden y reconstrucciones antes de animar."
            )
            if warnings:
                QMessageBox.warning(
                    self, "Revisión necesaria de la separación",
                    "La IA detectó señales de posible error:\n\n" + "\n".join(f"• {item}" for item in warnings)
                    + "\n\nLas capas se conservaron para que puedas corregirlas; no se descartó ninguna automáticamente.",
                )
            self.refresh_library()
        except (OSError, ProjectError) as exc:
            self._decomposition_failed(str(exc))

    def _decomposition_failed(self, message: str) -> None:
        self.status.setText("No se pudo completar la separación automática.")
        QMessageBox.warning(self, "Falló la separación con IA", message)

    def _decomposition_finished(self) -> None:
        self.decomposition_worker = None
        self._refresh_layer_list()

    def _sam2_proposals_ready(self, proposals: list[dict[str, Any]]) -> None:
        if not self.project_path:
            return
        self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
        self._refresh_proposals_for_current_layer(proposals[0]["id"] if proposals else None)
        self.status.setText(f"SAM 2 generó {len(proposals)} propuestas. Elige una y pulsa Aceptar propuesta.")
        self.refresh_library()

    def _sam2_failed(self, message: str) -> None:
        self.status.setText("No se pudo generar la máscara con SAM 2.")
        QMessageBox.warning(self, "Falló SAM 2", message)

    def _sam2_worker_finished(self) -> None:
        self.sam2_worker = None
        self.propose_mask_button.setEnabled(
            self.project_path is not None
            and self._selected_layer_id() is not None
            and not self._project_has_external_changes()
        )

    def _refresh_proposals_for_current_layer(self, preferred_proposal_id: str | None = None) -> None:
        self.proposal_combo.blockSignals(True)
        self.proposal_combo.clear()
        self.canvas.set_proposal_preview(None)
        layer_id = self.canvas.active_layer_id
        if self.project_path and layer_id:
            data = load_project(self.project_path)
            layer = next((entry for entry in data.get("layers", []) if entry.get("id") == layer_id), None)
            for index, proposal in enumerate(layer.get("mask_proposals", []) if layer else [], start=1):
                self.proposal_combo.addItem(
                    f"Propuesta {index} · {proposal.get('score', 0):.3f}",
                    proposal.get("id"),
                )
        has_proposals = self.proposal_combo.count() > 0
        self.proposal_combo.setEnabled(has_proposals)
        self.proposal_combo.blockSignals(False)
        selected_index = self.proposal_combo.findData(preferred_proposal_id) if preferred_proposal_id else -1
        self.proposal_combo.setCurrentIndex(selected_index if selected_index >= 0 else (0 if has_proposals else -1))
        self._select_mask_proposal(self.proposal_combo.currentIndex())
        if not has_proposals:
            self.accept_proposal_button.setEnabled(False)
        can_edit = self.project_path is not None and not self._project_has_external_changes()
        selected_layer = self._selected_layer_id() is not None
        for button in (self.positive_prompt_button, self.negative_prompt_button, self.box_prompt_button):
            button.setEnabled(can_edit and selected_layer)
        self.clear_prompts_button.setEnabled(can_edit and selected_layer)
        self.propose_mask_button.setEnabled(can_edit and selected_layer and self.sam2_worker is None)

    def _select_mask_proposal(self, index: int) -> None:
        if index < 0 or self.project_path is None or self.canvas.active_layer_id is None:
            self.canvas.set_proposal_preview(None)
            self.proposal_details.setText("SAM puntúa la calidad visual general; revisa que la propuesta corresponda a una sola pieza.")
            self.proposal_details.setStyleSheet("color: #aab4c5;")
            return
        proposal_id = self.proposal_combo.itemData(index)
        data = load_project(self.project_path)
        layer = next((entry for entry in data.get("layers", []) if entry.get("id") == self.canvas.active_layer_id), None)
        proposal = next((item for item in layer.get("mask_proposals", []) if item.get("id") == proposal_id), None) if layer else None
        path = self.project_path.parent / proposal["mask"] if proposal else None
        self.canvas.set_proposal_preview(path)
        if proposal and path and path.is_file():
            with PILImage.open(path) as opened:
                review = analyze_mask_proposal(opened, proposal)
            details = [f"Área: {review['area_ratio'] * 100:.1f}% de la imagen"]
            if review["connected_components"] is not None:
                details.append(f"Regiones: {review['connected_components']}")
            details.extend(review["warnings"])
            self.proposal_details.setText(" · ".join(details))
            self.proposal_details.setStyleSheet("color: #ff9e75;" if review["warnings"] else "color: #8fe0b1;")
            valid_prompts = review["positive_inside"] and review["negative_inside_count"] == 0
            self.accept_proposal_button.setEnabled(valid_prompts and not self._project_has_external_changes())
        else:
            self.accept_proposal_button.setEnabled(False)

    def _accept_mask_proposal(self) -> None:
        if self.project_path is None or self.canvas.active_layer_id is None:
            return
        proposal_id = self.proposal_combo.currentData()
        if not proposal_id:
            return
        try:
            if self._project_has_external_changes():
                raise ProjectError("El proyecto cambió mientras revisabas la propuesta. Recárgalo antes de aceptarla.")
            data = load_project(self.project_path)
            layer = next(item for item in data.get("layers", []) if item.get("id") == self.canvas.active_layer_id)
            proposal = next(item for item in layer.get("mask_proposals", []) if item.get("id") == proposal_id)
            with PILImage.open(self.project_path.parent / proposal["mask"]) as opened:
                review = analyze_mask_proposal(opened, proposal)
            if not review["positive_inside"] or review["negative_inside_count"]:
                raise ProjectError("Esta propuesta contradice los puntos de indicación. Elige otra o vuelve a generarla.")
            if review["warnings"]:
                answer = QMessageBox.warning(
                    self, "Revisa la máscara antes de aceptarla",
                    " · ".join(review["warnings"]) + "\n\n"
                    "Puedes marcar puntos negativos sobre las partes que quieras separar y volver a proponer la máscara. "
                    "¿Aceptar esta propuesta de todas formas?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            accept_layer_mask_proposal(self.project_path, self.canvas.active_layer_id, proposal_id)
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self.canvas.clear_prompts()
            self._open_project_file(self.project_path)
            self.status.setText("Propuesta aceptada como máscara editable. Los derivados de la versión anterior quedan marcados para rehacerse.")
            self.refresh_library()
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude aceptar la propuesta", str(exc))

    def toggle_mask_paint(self, enabled: bool) -> None:
        if enabled:
            self.canvas.prompt_mode = None
            self.prompt_group.setExclusive(False)
            for button in (self.positive_prompt_button, self.negative_prompt_button, self.box_prompt_button):
                button.setChecked(False)
            self.prompt_group.setExclusive(True)
        self.canvas.paint_mode = bool(enabled and self.canvas.mask_image is not None and not self._project_has_external_changes())
        self.paint_button.setText("Terminar pintura" if self.canvas.paint_mode else "Pintar máscara")
        if self.canvas.paint_mode:
            self.status.setText("Pinta con botón izquierdo; borra con botón derecho. Suelta para guardar la máscara.")
        elif self.project_path:
            self.status.setText(f"Proyecto: {self.project_path.name}")

    def _set_brush_size(self, value: int) -> None:
        self.canvas.brush_size = int(value)

    def _refresh_motion_controls(self, data: dict[str, Any] | None = None) -> None:
        if self.project_path is None:
            return
        data = data or load_project(self.project_path)
        old_clip = self.clip_combo.currentData()
        old_frame = self.current_frame
        self.clip_combo.blockSignals(True)
        self.clip_combo.clear()
        for clip in data.get("animations", []):
            self.clip_combo.addItem(clip.get("name", clip["id"]), clip["id"])
        clip_index = self.clip_combo.findData(old_clip) if old_clip else -1
        self.clip_combo.setCurrentIndex(clip_index if clip_index >= 0 else (0 if self.clip_combo.count() else -1))
        self.clip_combo.blockSignals(False)
        old_expression = self.expression_combo.currentData()
        self.expression_combo.blockSignals(True)
        self.expression_combo.clear()
        for expression in data.get("expressions", []):
            self.expression_combo.addItem(expression.get("name", expression["id"]), expression["id"])
        expression_index = self.expression_combo.findData(old_expression) if old_expression else -1
        self.expression_combo.setCurrentIndex(expression_index if expression_index >= 0 else -1)
        self.expression_combo.blockSignals(False)
        self._select_clip(self.clip_combo.currentIndex())
        if old_clip and self.clip_combo.currentData() == old_clip:
            self.frame_slider.setValue(min(old_frame, self.frame_slider.maximum()))
        editable = not self._project_has_external_changes()
        self.key_button.setEnabled(editable and self.canvas.active_layer_id is not None and self.clip_combo.count() > 0 and len(self.canvas.selected) == 1)
        self._refresh_layer_visibility_control()
        self.create_clip_button.setEnabled(editable)
        self.create_expression_button.setEnabled(editable)
        self._refresh_weight_controls()
        self._sync_selection(self.canvas.selected)

    def _refresh_weight_controls(self) -> None:
        layer = next((item for item in self.canvas.layers if item.get("id") == self.canvas.active_layer_id), None)
        mesh = layer.get("mesh") if layer else None
        has_mesh = bool(mesh and mesh.get("weights"))
        self.vertex_spin.blockSignals(True)
        self.vertex_spin.setRange(0, max(0, len(mesh.get("weights", [])) - 1) if has_mesh else 0)
        self.vertex_spin.setEnabled(has_mesh)
        self.vertex_spin.blockSignals(False)
        self.weight_joint_combo.blockSignals(True)
        old_joint = self.weight_joint_combo.currentData()
        self.weight_joint_combo.clear()
        for joint in self.canvas.joints:
            self.weight_joint_combo.addItem(joint.get("name", joint["id"]), joint["id"])
        selected_index = self.weight_joint_combo.findData(old_joint) if old_joint else -1
        self.weight_joint_combo.setCurrentIndex(selected_index if selected_index >= 0 else (0 if self.weight_joint_combo.count() else -1))
        self.weight_joint_combo.setEnabled(has_mesh and bool(self.canvas.joints))
        self.weight_joint_combo.blockSignals(False)
        vertex = self.vertex_spin.value()
        joint_id = self.weight_joint_combo.currentData()
        current = (mesh.get("weights", [])[vertex].get(joint_id, 0.0)
                   if has_mesh and joint_id and vertex < len(mesh["weights"]) else 0.0)
        self.weight_slider.blockSignals(True)
        self.weight_slider.setValue(round(current * 100))
        self.weight_slider.setEnabled(has_mesh and bool(joint_id))
        self.weight_slider.blockSignals(False)
        self.apply_weight_button.setEnabled(has_mesh and bool(joint_id) and not self._project_has_external_changes())

    def _apply_vertex_weight(self) -> None:
        if not self.project_path or not self.canvas.active_layer_id:
            return
        try:
            set_vertex_weight(self.project_path, self.canvas.active_layer_id, self.vertex_spin.value(),
                              self.weight_joint_combo.currentData(), self.weight_slider.value() / 100, actor="user")
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self.canvas.layers = load_project(self.project_path).get("layers", [])
            self._refresh_weight_controls()
            self._render_current_pose()
            self.status.setText(f"Pesos normalizados en el vértice {self.vertex_spin.value()}.")
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude guardar el peso", str(exc))

    def _set_ik_target(self, x: float, y: float) -> None:
        self.ik_pick_button.setChecked(False)
        self._sync_selection(self.canvas.selected)
        self.ik_status.setText(f"Objetivo: ({x:.0f}, {y:.0f}) px. Selecciona raíz, articulación media y extremo.")

    def _solve_ik_preview(self) -> None:
        if len(self.canvas.selected) != 3 or not self.canvas.ik_target:
            return
        try:
            bend = -1 if self.ik_opposite_bend.isChecked() else 1
            angles = solve_two_bone_ik(self.canvas.joints, *self.canvas.selected, *self.canvas.ik_target, bend)
            self.preview_rotations.update(angles)
            self.rotation_slider.blockSignals(True)
            selected_rotation = round(angles.get(self.canvas.selected[0], 0))
            self.rotation_slider.setValue(selected_rotation)
            self.rotation_slider.blockSignals(False)
            self.rotation_value.setText(f"{selected_rotation}°")
            self._render_current_pose()
            self.status.setText("IK resuelta como pose de previsualización; guarda claves para conservarla en el clip.")
        except ProjectError as exc:
            QMessageBox.warning(self, "No pude resolver IK", str(exc))

    def _generate_active_mesh(self) -> None:
        if self.project_path is None or self.canvas.active_layer_id is None:
            return
        try:
            if self._project_has_external_changes():
                raise ProjectError("Recarga el proyecto antes de generar una malla.")
            mesh = generate_layer_mesh(self.project_path, self.canvas.active_layer_id, actor="user")
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self.canvas.layers = load_project(self.project_path).get("layers", [])
            self._refresh_motion_controls()
            self.status.setText(f"Malla generada: {len(mesh['points'])} vértices, {len(mesh['triangles'])} triángulos.")
            self.refresh_library()
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude generar la malla", str(exc))

    def _create_clip(self) -> None:
        if self.project_path is None:
            return
        name, accepted = QInputDialog.getText(self, "Crear animación", "Nombre del clip:")
        if not accepted or not name.strip():
            return
        try:
            clip_id = create_animation_clip(self.project_path, name.strip(), duration=60, fps=30, actor="user")
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self._refresh_motion_controls()
            index = self.clip_combo.findData(clip_id)
            if index >= 0:
                self.clip_combo.setCurrentIndex(index)
            self.refresh_library()
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude crear el clip", str(exc))

    def _select_clip(self, index: int) -> None:
        self.preview_layer_visibility.clear()
        clip_id = self.clip_combo.itemData(index) if index >= 0 else None
        clip = None
        if clip_id and self.project_path:
            data = load_project(self.project_path)
            clip = next((item for item in data.get("animations", []) if item.get("id") == clip_id), None)
        self.current_frame = 0
        self.frame_slider.blockSignals(True)
        self.frame_slider.setRange(0, int(clip.get("duration", 60)) if clip else 60)
        self.frame_slider.setValue(0)
        self.frame_slider.blockSignals(False)
        self.key_button.setEnabled(bool(clip and len(self.canvas.selected) == 1 and not self._project_has_external_changes()))
        if clip:
            self._update_playback_interval(clip)
        self._refresh_layer_visibility_control()
        self._render_current_pose()

    def _set_animation_frame(self, frame: int) -> None:
        self.current_frame = int(frame)
        self.preview_layer_visibility.clear()
        self.frame_label.setText(f"Fotograma {frame}")
        self._refresh_layer_visibility_control()
        self._render_current_pose()

    def _set_playback_direction(self, reverse: bool) -> None:
        self.playback_direction = -1 if reverse else 1

    def _step_animation_frame(self, amount: int) -> None:
        if self.playing:
            self.play_button.setChecked(False)
        self.frame_slider.setValue(max(self.frame_slider.minimum(), min(
            self.frame_slider.maximum(), self.frame_slider.value() + amount
        )))

    def _go_to_frame_edge(self, end: bool) -> None:
        if self.playing:
            self.play_button.setChecked(False)
        self.frame_slider.setValue(self.frame_slider.maximum() if end else self.frame_slider.minimum())

    def _set_playback_speed(self, _index: int = -1) -> None:
        if not hasattr(self, "playback_timer"):
            return
        clip_id = self.clip_combo.currentData()
        if not clip_id or not self.project_path:
            return
        data = load_project(self.project_path)
        clip = next((item for item in data.get("animations", []) if item.get("id") == clip_id), None)
        if clip:
            self._update_playback_interval(clip)

    def _update_playback_interval(self, clip: dict[str, Any]) -> None:
        fps = max(1.0, float(clip.get("fps", 30)))
        speed = float(self.playback_speed_combo.currentData() or 1.0)
        self.playback_timer.setInterval(max(1, round(1000 / (fps * speed))))

    def _refresh_layer_visibility_control(self) -> None:
        if not hasattr(self, "layer_visibility_checkbox"):
            return
        layer_id = self.canvas.active_layer_id
        layer = next((item for item in self.canvas.layers if item.get("id") == layer_id), None)
        visible = bool(layer.get("visible", True)) if layer else True
        clip_id = self.clip_combo.currentData() if hasattr(self, "clip_combo") else None
        if clip_id and self.project_path:
            data = load_project(self.project_path)
            clip = next((item for item in data.get("animations", []) if item.get("id") == clip_id), None)
            if clip:
                frame = self.current_frame % clip["duration"] if clip.get("loop") and clip["duration"] else self.current_frame
                visible = sample_layer_visibility(clip, frame).get(layer_id, visible)
        visible = self.preview_layer_visibility.get(layer_id, visible) if layer_id else visible
        self.layer_visibility_checkbox.blockSignals(True)
        self.layer_visibility_checkbox.setChecked(visible)
        self.layer_visibility_checkbox.blockSignals(False)
        editable = bool(layer and clip_id and not self._project_has_external_changes())
        self.layer_visibility_checkbox.setEnabled(bool(layer and not self._project_has_external_changes()))
        self.layer_visibility_key_button.setEnabled(editable)

    def _preview_layer_visibility(self, visible: bool) -> None:
        layer_id = self.canvas.active_layer_id
        if layer_id:
            self.preview_layer_visibility[layer_id] = bool(visible)
            self._render_current_pose()

    def _save_layer_visibility_key(self) -> None:
        layer_id = self.canvas.active_layer_id
        clip_id = self.clip_combo.currentData()
        if not self.project_path or not layer_id or not clip_id:
            return
        try:
            if self._project_has_external_changes():
                raise ProjectError("Recarga el proyecto antes de guardar una clave.")
            set_layer_visibility_key(
                self.project_path, clip_id, layer_id, self.current_frame,
                self.layer_visibility_checkbox.isChecked(), actor="user",
            )
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self.preview_layer_visibility.pop(layer_id, None)
            self._refresh_layer_visibility_control()
            self._render_current_pose()
            self.status.setText(f"Clave de visibilidad guardada en el fotograma {self.current_frame}.")
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude guardar la visibilidad", str(exc))

    def _toggle_playback(self, playing: bool) -> None:
        if playing and not self.clip_combo.currentData():
            self.play_button.setChecked(False)
            QMessageBox.information(self, "Sin clip", "Crea un clip y añade claves antes de reproducir.")
            return
        self.playing = bool(playing)
        self.preview_layer_visibility.clear()
        self.play_button.setText("■ Detener" if playing else "▶ Reproducir")
        if playing:
            self.playback_clock.start()
            self.playback_last_elapsed_ms = 0
            self.playback_frame_remainder = 0.0
            self.playback_timer.start()
        else:
            self.playback_timer.stop()
            self._render_current_pose()

    def _advance_animation_frame(self) -> None:
        if self.project_path is None or not self.clip_combo.currentData():
            self.play_button.setChecked(False)
            return
        data = load_project(self.project_path)
        clip = next((item for item in data.get("animations", []) if item.get("id") == self.clip_combo.currentData()), None)
        if clip is None:
            self.play_button.setChecked(False)
            return
        elapsed_ms = self.playback_clock.elapsed()
        elapsed_delta = max(0, elapsed_ms - self.playback_last_elapsed_ms)
        self.playback_last_elapsed_ms = elapsed_ms
        fps = max(1.0, float(clip.get("fps", 30)))
        speed = float(self.playback_speed_combo.currentData() or 1.0)
        advanced = self.playback_frame_remainder + elapsed_delta * fps * speed / 1000.0
        frame_steps = int(advanced)
        self.playback_frame_remainder = advanced - frame_steps
        if frame_steps == 0:
            return

        duration = int(clip["duration"])
        next_frame = self.current_frame + self.playback_direction * frame_steps
        if clip.get("loop", True):
            next_frame %= duration + 1
        elif next_frame > duration or next_frame < 0:
            next_frame = duration if self.playback_direction > 0 else 0
            self.playback_timer.stop()
            self.playing = False
            self.play_button.blockSignals(True)
            self.play_button.setChecked(False)
            self.play_button.setText("▶ Reproducir")
            self.play_button.blockSignals(False)
        self.frame_slider.setValue(next_frame)

    def _preview_joint_rotation(self, value: int) -> None:
        selected = self.canvas.selected
        self.rotation_value.setText(f"{value}°")
        if len(selected) != 1:
            return
        self.preview_rotations[selected[0]] = float(value)
        if self.playing:
            self.play_button.setChecked(False)
        self._render_current_pose()

    def _save_rotation_key(self) -> None:
        if self.project_path is None or len(self.canvas.selected) != 1 or not self.clip_combo.currentData():
            return
        try:
            if self._project_has_external_changes():
                raise ProjectError("Recarga el proyecto antes de guardar una clave.")
            set_rotation_key(
                self.project_path, self.clip_combo.currentData(), self.canvas.selected[0],
                self.current_frame, float(self.rotation_slider.value()),
                interpolation=self.interpolation_combo.currentData(), actor="user",
            )
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self._refresh_motion_controls()
            self.status.setText(f"Clave guardada en el fotograma {self.current_frame}.")
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude guardar la clave", str(exc))

    def _preview_expression(self, _index: int) -> None:
        self.preview_layer_opacities.clear()
        expression_id = self.expression_combo.currentData()
        layer_id = self.canvas.active_layer_id
        opacity = 1.0
        if expression_id and self.project_path:
            expression = next((item for item in load_project(self.project_path).get("expressions", [])
                               if item.get("id") == expression_id), None)
            if expression and layer_id:
                opacity = float(expression.get("layer_opacities", {}).get(layer_id, 1.0))
        self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(round(opacity * 100))
        self.opacity_slider.blockSignals(False)
        self._render_current_pose()

    def _create_expression_from_pose(self) -> None:
        if self.project_path is None:
            return
        name, accepted = QInputDialog.getText(self, "Crear expresión", "Nombre de la expresión:")
        if not accepted or not name.strip():
            return
        try:
            expression_id = create_expression(
                self.project_path, name.strip(), self.preview_rotations,
                self.preview_layer_opacities, actor="user",
            )
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self._refresh_motion_controls()
            self.expression_combo.setCurrentIndex(self.expression_combo.findData(expression_id))
            self.refresh_library()
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude crear la expresión", str(exc))

    def _preview_layer_opacity(self, value: int) -> None:
        layer_id = self.canvas.active_layer_id
        if layer_id:
            self.preview_layer_opacities[layer_id] = value / 100.0
            self._render_current_pose()

    def _save_expression_opacity(self) -> None:
        if self.project_path is None or not self.expression_combo.currentData() or not self.canvas.active_layer_id:
            return
        try:
            set_expression_value(
                self.project_path, self.expression_combo.currentData(), self.canvas.active_layer_id,
                self.opacity_slider.value() / 100.0, "layer_opacity", actor="user",
            )
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self._refresh_motion_controls()
            self.status.setText("Opacidad guardada en la expresión.")
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude guardar la expresión", str(exc))

    def _save_ik_keys(self) -> None:
        if (self.project_path is None or len(self.canvas.selected) != 3 or not self.canvas.ik_target
                or not self.clip_combo.currentData()):
            return
        try:
            if self._project_has_external_changes():
                raise ProjectError("Recarga el proyecto antes de guardar claves IK.")
            bend = -1 if self.ik_opposite_bend.isChecked() else 1
            angles = solve_two_bone_ik(self.canvas.joints, *self.canvas.selected, *self.canvas.ik_target, bend)
            for joint_id, angle in angles.items():
                set_rotation_key(self.project_path, self.clip_combo.currentData(), joint_id,
                                 self.current_frame, angle, actor="user")
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self._refresh_motion_controls()
            self.status.setText(f"Claves IK guardadas en el fotograma {self.current_frame}.")
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude guardar las claves IK", str(exc))

    def _toggle_behavior_simulation(self, enabled: bool) -> None:
        if enabled and self.project_path is None:
            self.behavior_enabled.blockSignals(True)
            self.behavior_enabled.setChecked(False)
            self.behavior_enabled.blockSignals(False)
            self.status.setText("Abre un proyecto antes de simular el comportamiento.")
            return
        if enabled:
            if self.playing:
                self.play_button.setChecked(False)
            try:
                self.behavior_runtime = BehaviorRuntime(load_project(self.project_path))
                self.behavior_runtime.dispatch({"type": "state", "state": self.behavior_combo.currentData(), "intensity": self.behavior_intensity.value() / 100})
                self.behavior_clock.start()
                self.behavior_last_elapsed_ms = 0
                self.behavior_timer.start()
                self.status.setText("Simulación reactiva activa · los eventos y la pose son temporales.")
            except (OSError, ProjectError) as exc:
                self.behavior_enabled.setChecked(False)
                self.status.setText(f"No pude iniciar la simulación: {exc}")
        else:
            self.behavior_timer.stop()
            self.behavior_runtime = None
            self._render_current_pose()

    def _set_behavior_state(self, _index: int = -1) -> None:
        if self.behavior_runtime:
            self.behavior_runtime.dispatch({"type": "state", "state": self.behavior_combo.currentData(), "intensity": self.behavior_intensity.value() / 100})

    def _set_behavior_intensity(self, value: int) -> None:
        if self.behavior_runtime:
            self.behavior_runtime.dispatch({"type": "parameter", "name": "intensity", "value": value / 100})

    def _advance_behavior_simulation(self) -> None:
        if not self.behavior_runtime or not self.project_path:
            self.behavior_timer.stop()
            return
        elapsed_ms = self.behavior_clock.elapsed()
        dt = max(0.0, (elapsed_ms - self.behavior_last_elapsed_ms) / 1000.0)
        self.behavior_last_elapsed_ms = elapsed_ms
        self.behavior_runtime.update(dt)
        self._render_current_pose()

    def _render_current_pose(self) -> None:
        if self.project_path is None:
            return
        try:
            data = load_project(self.project_path)
            rotations: dict[str, float] = {}
            layer_visibility: dict[str, bool] = {}
            facial_parameters = None
            translation = (0.0, 0.0)
            if self.behavior_runtime:
                behavior_pose = self.behavior_runtime.pose
                rotations.update(behavior_pose["rotations"])
                layer_visibility.update(behavior_pose["layer_visibility"])
                facial_parameters = behavior_pose["facial_parameters"]
                translation = behavior_pose.get("translation", translation)
            clip_id = self.clip_combo.currentData()
            if clip_id and not self.behavior_runtime:
                clip = next((item for item in data.get("animations", []) if item.get("id") == clip_id), None)
                if clip:
                    frame = self.current_frame % clip["duration"] if self.playing and clip.get("loop") and clip["duration"] else self.current_frame
                    rotations.update(sample_clip(clip, frame))
                    layer_visibility.update(sample_layer_visibility(clip, frame))
            if not self.playing:
                rotations.update(self.preview_rotations)
                layer_visibility.update(self.preview_layer_visibility)
            image = render_project_pose(
                self.project_path, rotations, self.preview_layer_opacities,
                self.expression_combo.currentData(), layer_visibility, facial_parameters, translation,
            ).convert("RGBA")
            qimage = QImage(image.tobytes(), image.width, image.height, image.width * 4, QImage.Format.Format_RGBA8888).copy()
            self.canvas.composite_preview = QPixmap.fromImage(qimage)
            self.canvas.preview_joint_positions = posed_joint_positions(
                data.get("joints", []), rotations
            )
            if translation != (0.0, 0.0):
                self.canvas.preview_joint_positions = {
                    joint_id: (position[0] + translation[0], position[1] + translation[1])
                    for joint_id, position in self.canvas.preview_joint_positions.items()
                }
            self.canvas.preview_enabled = True
            if not self.preview_button.isChecked():
                self.preview_button.setChecked(True)
            self.canvas.viewport().update()
        except (OSError, ProjectError):
            # A skeleton without extracted pieces remains editable; validation explains what is missing.
            pass

    def _project_has_external_changes(self) -> bool:
        return bool(
            self.project_path
            and self.project_path.exists()
            and self.loaded_mtime_ns is not None
            and self.project_path.stat().st_mtime_ns != self.loaded_mtime_ns
        )

    def extract_active_layer(self) -> None:
        if self.project_path is None or self.canvas.active_layer_id is None:
            return
        try:
            if self.loaded_mtime_ns is not None and self.project_path.stat().st_mtime_ns != self.loaded_mtime_ns:
                raise ProjectError("El proyecto cambió desde que lo abriste. Recárgalo antes de extraer.")
            sprite = extract_layer(self.project_path, self.canvas.active_layer_id, actor="user")
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self._refresh_composite_preview()
            self.status.setText(f"Pieza extraída: {sprite.name}. Revisa la composición antes de continuar.")
            self.refresh_library()
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude extraer la pieza", str(exc))

    def reconstruct_hidden_areas(self) -> None:
        if self.project_path is None:
            return
        try:
            if self.loaded_mtime_ns is not None and self.project_path.stat().st_mtime_ns != self.loaded_mtime_ns:
                raise ProjectError("El proyecto cambió desde que lo abriste. Recárgalo antes de reconstruir.")
            result = inpaint_layer_holes(self.project_path, actor="user")
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self._refresh_composite_preview()
            self.preview_button.setChecked(True)
            self.status.setText(f"Zonas reconstruidas con inpainting clásico: {result.name}. Revisa el resultado.")
            self.refresh_library()
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude reconstruir las zonas", str(exc))

    def _refresh_composite_preview(self, show_error: bool = True) -> None:
        if self.project_path is None:
            return
        try:
            composite = render_composite(self.project_path).convert("RGBA")
            image = QImage(
                composite.tobytes(), composite.width, composite.height,
                composite.width * 4, QImage.Format.Format_RGBA8888,
            ).copy()
            self.canvas.composite_preview = QPixmap.fromImage(image)
            self.preview_button.setEnabled(True)
            self.canvas.viewport().update()
        except (OSError, ProjectError) as exc:
            self.canvas.composite_preview = None
            self.preview_button.setEnabled(False)
            self.preview_button.setChecked(False)
            if show_error:
                QMessageBox.warning(self, "No pude componer las piezas", str(exc))

    def _toggle_composite_preview(self, enabled: bool) -> None:
        self.canvas.preview_enabled = bool(enabled and self.canvas.composite_preview)
        self.canvas.viewport().update()

    def _commit_mask_change(self, layer_id: str) -> None:
        if self.project_path is None:
            return
        try:
            if self.loaded_mtime_ns is not None and self.project_path.stat().st_mtime_ns != self.loaded_mtime_ns:
                self.reload_button.setEnabled(True)
                self.canvas.paint_mode = False
                self.paint_button.setChecked(False)
                self.paint_button.setEnabled(False)
                QMessageBox.warning(self, "Cambios externos", "El proyecto cambió durante la edición. Recárgalo para sincronizar el manifiesto.")
                return
            if self.canvas.mask_image is None or self.canvas.mask_path is None:
                raise ProjectError("No hay una máscara activa para guardar.")
            self.canvas.mask_image.save(self.canvas.mask_path, format="PNG")
            save_project(self.project_path, self._editor_project_data(), action=f"layer_mask_painted:{layer_id}", actor="user")
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self.canvas.composite_preview = None
            self.preview_button.setChecked(False)
            self.preview_button.setEnabled(False)
            self.extract_layer_button.setEnabled(True)
            self.status.setText("Máscara guardada.")
            self.refresh_library()
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude guardar la máscara", str(exc))

    def reload_current_project(self) -> None:
        if self.project_path is None:
            return
        try:
            self._open_project_file(self.project_path)
            self.status.setText(f"Recargado: {self.project_path.name}")
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude recargar", str(exc))

    def save_project(self) -> None:
        if self.canvas.image is None:
            return
        project_path = self.project_path
        if project_path is None:
            suggested = str(Path.home() / "RigM" / "Projects" / f"{self.canvas.image_path.stem}.rigm")
            path, _ = QFileDialog.getSaveFileName(self, "Guardar proyecto RigM", suggested, PROJECT_FILTER)
            if not path:
                return
            project_path = Path(path)
            if project_path.suffix.lower() != ".rigm":
                project_path = project_path.with_suffix(".rigm")
        try:
            if self.project_path and self.loaded_mtime_ns is not None and self.project_path.exists():
                if self.project_path.stat().st_mtime_ns != self.loaded_mtime_ns:
                    QMessageBox.warning(self, "Cambios externos", "El proyecto cambió desde que lo abriste. Recárgalo para no sobrescribir el trabajo externo.")
                    self.reload_button.setEnabled(True)
                    return
            data = self._editor_project_data(project_path)
            data["name"] = project_path.stem
            save_project(project_path, data, action="editor_save", actor="user")
            self.project_path = project_path.resolve()
            self.library.add(project_path)
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            self.reload_button.setEnabled(False)
            self.status.setText(f"Guardado: {project_path.name}")
            self.refresh_library()
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude guardar", str(exc))

    def _editor_project_data(self, path: Path | None = None) -> dict[str, Any]:
        path = path or self.project_path
        current = load_project(path) if path is not None and path.exists() else {}
        current.update({
            "format": "rigm", "version": 2,
            "image": str(self.canvas.image_path),
            "image_size": [self.canvas.image.width(), self.canvas.image.height()],
            "joints": self.canvas.joints, "bones": self.canvas.bones, "layers": self.canvas.layers,
        })
        return current

    def export_current_project(self) -> None:
        if self.project_path is None:
            QMessageBox.information(self, "Guarda primero", "Guarda el proyecto antes de exportar el bundle.")
            return
        suggested = self.project_path.with_suffix(".zip")
        output, _ = QFileDialog.getSaveFileName(self, "Exportar RigM portable", str(suggested), "Bundle RigM (*.zip)")
        if not output:
            return
        try:
            destination = Path(output).with_suffix(".zip")
            save_project(self.project_path, self._editor_project_data(), action="editor_save_before_export", actor="user")
            self.loaded_mtime_ns = self.project_path.stat().st_mtime_ns
            export_bundle(self.project_path, destination)
            self.status.setText(f"Bundle portable: {destination}")
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "No pude exportar", str(exc))

    def start_adding_joint(self) -> None:
        self.canvas.inserting = True
        self.status.setText("Haz clic en la ilustración donde quieras colocar el pivote.")

    def connect_selected(self) -> None:
        if len(self.canvas.selected) != 2:
            self.status.setText("Selecciona exactamente dos pivotes (Ctrl + clic).")
            return
        edge = list(self.canvas.selected)
        if edge[0] == edge[1] or any(set(existing) == set(edge) for existing in self.canvas.bones):
            self.status.setText("Ese hueso ya existe.")
            return
        by_id = {joint["id"]: joint for joint in self.canvas.joints}
        child = by_id[edge[1]]
        if child.get("parent") not in (None, edge[0]):
            self.status.setText("El pivote final ya tiene un padre. Quita primero su hueso actual.")
            return
        cursor = edge[0]
        while cursor:
            if cursor == edge[1]:
                self.status.setText("Ese hueso crearía un ciclo en la jerarquía.")
                return
            cursor = by_id.get(cursor, {}).get("parent")
        self.canvas.bones.append(edge)
        child["parent"] = edge[0]
        self.canvas.changed.emit()
        self.canvas.viewport().update()
        self.status.setText("Hueso conectado.")

    def remove_selected(self) -> None:
        if not self.canvas.selected:
            return
        removed = set(self.canvas.selected)
        for joint in self.canvas.joints:
            if joint.get("parent") in removed:
                joint["parent"] = None
        self.canvas.joints = [joint for joint in self.canvas.joints if joint["id"] not in removed]
        self.canvas.bones = [edge for edge in self.canvas.bones if not removed.intersection(edge)]
        self.canvas.selected = []
        for item in self.joint_list.selectedItems():
            self.joint_list.takeItem(self.joint_list.row(item))
        self.canvas.joint_selected.emit([])
        self.canvas.changed.emit()
        self.canvas.viewport().update()

    def _add_joint_row(self, joint_id: str) -> None:
        joint = next(j for j in self.canvas.joints if j["id"] == joint_id)
        item = QListWidgetItem(joint["name"])
        item.setData(Qt.ItemDataRole.UserRole, joint_id)
        self.joint_list.addItem(item)
        item.setSelected(True)

    def _select_from_list(self) -> None:
        self.canvas.selected = [item.data(Qt.ItemDataRole.UserRole) for item in self.joint_list.selectedItems()]
        self.canvas.joint_selected.emit(self.canvas.selected)
        self.canvas.viewport().update()

    def _sync_selection(self, joint_ids: list[str]) -> None:
        self.joint_list.blockSignals(True)
        for i in range(self.joint_list.count()):
            item = self.joint_list.item(i)
            item.setSelected(item.data(Qt.ItemDataRole.UserRole) in joint_ids)
        self.joint_list.blockSignals(False)
        self.connect_button.setEnabled(len(joint_ids) == 2)
        self.remove_button.setEnabled(bool(joint_ids))
        self.rotation_slider.setEnabled(len(joint_ids) == 1)
        if len(joint_ids) == 1:
            self.rotation_slider.blockSignals(True)
            self.rotation_slider.setValue(round(self.preview_rotations.get(joint_ids[0], 0.0)))
            self.rotation_slider.blockSignals(False)
            self.rotation_value.setText(f"{self.rotation_slider.value()}°")
        self.key_button.setEnabled(len(joint_ids) == 1 and self.clip_combo.count() > 0 and not self._project_has_external_changes())
        valid_chain = False
        if len(joint_ids) == 3:
            by_id = {joint["id"]: joint for joint in self.canvas.joints}
            valid_chain = (joint_ids[1] in by_id and joint_ids[2] in by_id
                           and by_id[joint_ids[1]].get("parent") == joint_ids[0]
                           and by_id[joint_ids[2]].get("parent") == joint_ids[1])
        self.ik_pick_button.setEnabled(valid_chain)
        self.solve_ik_button.setEnabled(valid_chain and self.canvas.ik_target is not None)
        self.save_ik_button.setEnabled(valid_chain and self.canvas.ik_target is not None
                                        and bool(self.clip_combo.currentData())
                                        and not self._project_has_external_changes())
        self.ik_status.setText("Cadena lista: raíz → medio → extremo." if valid_chain else
                               "Selecciona raíz, codo/rodilla y extremo en orden con Ctrl.")

    def _refresh_status(self) -> None:
        if self.canvas.image:
            self.add_button.setEnabled(True)
            self.save_button.setEnabled(True)

    def _set_ready(self, ready: bool) -> None:
        self.add_button.setEnabled(ready)
        self.save_button.setEnabled(ready)
        self.export_button.setEnabled(ready and self.project_path is not None)
        self._refresh_status()

    def closeEvent(self, event) -> None:
        if self.sam2_worker is not None and self.sam2_worker.isRunning():
            self.sam2_worker.wait()
        if self.decomposition_worker is not None and self.decomposition_worker.isRunning():
            self.decomposition_worker.wait()
        self.library_timer.stop()
        self.playback_timer.stop()
        self.behavior_timer.stop()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
