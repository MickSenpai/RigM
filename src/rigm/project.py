"""Persistent project model and operations shared by the editor and agents."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image
from PIL import ImageChops


class ProjectError(ValueError):
    pass


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def skeleton_signature(data: dict[str, Any]) -> str:
    joints = sorted(
        ({"id": item.get("id"), "x": float(item.get("x", 0)), "y": float(item.get("y", 0)), "parent": item.get("parent")} for item in data.get("joints", [])),
        key=lambda item: str(item["id"]),
    )
    encoded = json.dumps(joints, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectError(f"No pude leer {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != "rigm":
        raise ProjectError("El archivo no es un proyecto RigM.")
    if data.get("version") not in (1, 2):
        raise ProjectError(f"Versión de proyecto no compatible: {data.get('version')}.")
    return data


def resolve_image(data: dict[str, Any], project_path: Path) -> Path:
    image_path = Path(data["image"])
    if not image_path.is_absolute():
        image_path = project_path.parent / image_path
    return image_path.resolve()


def load_project(path: str | Path) -> dict[str, Any]:
    project_path = Path(path).expanduser().resolve()
    data = _read_json(project_path)
    data.setdefault("id", str(uuid.uuid4()))
    data.setdefault("name", project_path.stem)
    data.setdefault("status", "in_progress")
    data.setdefault("stage", "skeleton")
    data.setdefault("created_at", now_utc())
    data.setdefault("updated_at", now_utc())
    data.setdefault("history", [])
    data.setdefault("animations", [])
    data.setdefault("expressions", [])
    if isinstance(data.get("joints"), list):
        parents = {joint.get("id"): joint.get("parent") for joint in data["joints"] if joint.get("parent")}
        for edge in data.get("bones", []):
            if isinstance(edge, list) and len(edge) == 2 and edge[1] not in parents:
                parents[edge[1]] = edge[0]
        for joint in data["joints"]:
            if not joint.get("parent") and joint.get("id") in parents:
                joint["parent"] = parents[joint["id"]]
            else:
                joint.setdefault("parent", None)
            joint.setdefault("rotation", 0.0)
    data["_path"] = str(project_path)
    data["_image_path"] = str(resolve_image(data, project_path))
    return data


def _portable_image(data: dict[str, Any], project_path: Path) -> str:
    path_value = data.get("_image_path") or data.get("image")
    source = Path(path_value).expanduser()
    if not source.is_absolute():
        source = project_path.parent / source
    source = source.resolve()
    if not source.is_file():
        raise ProjectError(f"No encuentro la imagen de referencia: {source}")
    assets = project_path.parent / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    destination = assets / f"reference{source.suffix.lower()}"
    if source != destination.resolve():
        shutil.copy2(source, destination)
    return destination.relative_to(project_path.parent).as_posix()


def save_project(
    path: str | Path,
    data: dict[str, Any],
    *,
    action: str = "save",
    actor: str = "user",
) -> Path:
    project_path = Path(path).expanduser().resolve()
    project_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in data.items() if not key.startswith("_")}
    payload["format"] = "rigm"
    payload["version"] = 2
    payload.setdefault("id", str(uuid.uuid4()))
    payload.setdefault("name", project_path.stem)
    payload.setdefault("created_at", now_utc())
    payload["updated_at"] = now_utc()
    payload["image"] = _portable_image(data, project_path)
    payload.setdefault("status", "in_progress")
    payload.setdefault("stage", "skeleton")
    payload.setdefault("progress", 0)
    history = list(payload.get("history", []))
    history.append({"at": payload["updated_at"], "action": action, "actor": actor})
    payload["history"] = history[-100:]
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    handle, temp_name = tempfile.mkstemp(prefix=f".{project_path.name}.", suffix=".tmp", dir=project_path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, project_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return project_path


def create_project(image: str | Path, output: str | Path, name: str | None = None) -> Path:
    image_path = Path(image).expanduser().resolve()
    if not image_path.is_file():
        raise ProjectError(f"No encuentro la imagen: {image_path}")
    try:
        with Image.open(image_path) as source_image:
            image_size = list(source_image.size)
    except OSError as exc:
        raise ProjectError(f"No pude leer la imagen: {exc}") from exc
    project_path = Path(output).expanduser().resolve()
    data: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "name": name or project_path.stem,
        "image": str(image_path),
        "image_size": image_size,
        "joints": [],
        "bones": [],
        "layers": [],
        "status": "in_progress",
        "stage": "source",
        "created_at": now_utc(),
        "history": [],
    }
    return save_project(project_path, data, action="project_created", actor="user")


def _new_layer(data: dict[str, Any], project_path: Path, name: str, mask: Image.Image, actor: str) -> str:
    source_size = tuple(data.get("image_size") or mask.size)
    if mask.size != source_size:
        raise ProjectError(f"La máscara mide {mask.width}×{mask.height}; la imagen mide {source_size[0]}×{source_size[1]}.")
    layer_id = f"layer-{uuid.uuid4().hex[:12]}"
    relative_mask = Path("assets") / "masks" / f"{layer_id}.png"
    mask_path = project_path.parent / relative_mask
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    mask.save(mask_path, format="PNG")
    data.setdefault("layers", []).append({"id": layer_id, "name": name, "mask": relative_mask.as_posix(), "visible": True})
    data["stage"] = "layers"
    data["status"] = "in_progress"
    save_project(project_path, data, action="layer_added", actor=actor)
    return layer_id


def create_layer(path: str | Path, name: str, actor: str = "agent") -> str:
    project_path = Path(path).expanduser().resolve()
    data = load_project(project_path)
    image_path = Path(data["_image_path"])
    try:
        with Image.open(image_path) as image:
            size = tuple(data.get("image_size") or image.size)
    except OSError as exc:
        raise ProjectError(f"No pude leer la imagen de referencia: {exc}") from exc
    if not name.strip():
        raise ProjectError("La capa necesita un nombre.")
    layer_id = f"layer-{uuid.uuid4().hex[:12]}"
    relative_mask = Path("assets") / "masks" / f"{layer_id}.png"
    mask_path = project_path.parent / relative_mask
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("L", size, 0).save(mask_path, format="PNG")
    data.setdefault("layers", []).append({"id": layer_id, "name": name.strip(), "mask": relative_mask.as_posix(), "visible": True})
    data["stage"], data["status"] = "layers", "in_progress"
    save_project(project_path, data, action="layer_added", actor=actor)
    return layer_id


def import_layer_mask(path: str | Path, name: str, mask_path: str | Path, actor: str = "agent") -> str:
    project_path = Path(path).expanduser().resolve()
    data = load_project(project_path)
    try:
        with Image.open(mask_path) as source_mask:
            mask = source_mask.convert("L")
    except OSError as exc:
        raise ProjectError(f"No pude leer la máscara: {exc}") from exc
    if not name.strip():
        raise ProjectError("La capa necesita un nombre.")
    return _new_layer(data, project_path, name.strip(), mask, actor)


def propose_layer_masks(
    path: str | Path,
    layer_id: str,
    positive_points: list[tuple[float, float]],
    negative_points: list[tuple[float, float]] | None = None,
    box: tuple[float, float, float, float] | None = None,
    model_id: str = "facebook/sam2.1-hiera-tiny",
    actor: str = "agent",
) -> list[dict[str, Any]]:
    """Run local SAM 2 and save ranked masks as reviewable, non-destructive proposals."""
    project_path = Path(path).expanduser().resolve()
    data = load_project(project_path)
    project_mtime = project_path.stat().st_mtime_ns
    layer = next((item for item in data.get("layers", []) if item.get("id") == layer_id), None)
    if layer is None:
        raise ProjectError(f"No existe la capa {layer_id}.")
    try:
        from .segmentation import propose_masks
        candidates = propose_masks(data["_image_path"], positive_points, negative_points, box, model_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ProjectError(str(exc)) from exc
    if project_path.stat().st_mtime_ns != project_mtime:
        raise ProjectError("El proyecto cambió mientras SAM 2 calculaba las propuestas; no las guardé.")
    proposal_dir = project_path.parent / "assets" / "mask-proposals"
    proposal_dir.mkdir(parents=True, exist_ok=True)
    proposals = layer.setdefault("mask_proposals", [])
    saved: list[dict[str, Any]] = []
    for mask, score in candidates:
        proposal_id = f"proposal-{uuid.uuid4().hex[:12]}"
        relative = Path("assets") / "mask-proposals" / f"{proposal_id}.png"
        mask.save(project_path.parent / relative, format="PNG")
        proposal = {
            "id": proposal_id,
            "mask": relative.as_posix(),
            "score": score,
            "model": model_id,
            "positive_points": [list(point) for point in positive_points],
            "negative_points": [list(point) for point in (negative_points or [])],
            "box": list(box) if box else None,
            "created_at": now_utc(),
        }
        proposals.append(proposal)
        saved.append(proposal)
    save_project(project_path, data, action=f"mask_proposals_generated:{layer_id}", actor=actor)
    return saved


def analyze_mask_proposal(mask: Image.Image, proposal: dict[str, Any]) -> dict[str, Any]:
    """Return simple review signals so SAM's IoU score is not mistaken for part correctness."""
    binary = mask.convert("L").point(lambda value: 255 if value >= 128 else 0)
    width, height = binary.size
    histogram = binary.histogram()
    area = sum(histogram[1:])
    area_ratio = area / max(1, width * height)

    def point_is_inside(point: list[float] | tuple[float, float]) -> bool:
        x, y = int(round(float(point[0]))), int(round(float(point[1])))
        return 0 <= x < width and 0 <= y < height and binary.getpixel((x, y)) > 0

    positives = proposal.get("positive_points", [])
    negatives = proposal.get("negative_points", [])
    positive_inside = all(point_is_inside(point) for point in positives)
    negative_inside_count = sum(point_is_inside(point) for point in negatives)
    component_count = None
    try:
        import cv2
        import numpy as np
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
            np.asarray(binary, dtype=np.uint8), connectivity=8,
        )
        minimum_component = max(24, round(width * height * 0.0001))
        component_count = sum(1 for row in stats[1:count] if int(row[cv2.CC_STAT_AREA]) >= minimum_component)
    except ImportError:
        pass

    warnings: list[str] = []
    if positives and not positive_inside:
        warnings.append("La máscara deja fuera el punto que debía conservar.")
    if negative_inside_count:
        warnings.append(f"La máscara incluye {negative_inside_count} punto(s) que marcaste para excluir.")
    if area_ratio >= 0.24:
        warnings.append("Cubre casi una cuarta parte de la imagen; probablemente mezcla varias piezas.")
    elif area_ratio >= 0.10:
        warnings.append("Es una máscara grande para una pieza; revisa si incluye torso, brazos o piernas juntos.")
    elif area_ratio < 0.005:
        warnings.append("Es muy pequeña; podría ser sólo un detalle y no una pieza animable completa.")
    if component_count is not None and component_count > 1:
        warnings.append(f"Tiene {component_count} regiones desconectadas; confirma que todas pertenecen a la misma pieza.")
    box = proposal.get("box")
    box_coverage = None
    if box and area:
        x1, y1, x2, y2 = [int(round(float(value))) for value in box]
        x1, x2 = max(0, x1), min(width, x2)
        y1, y2 = max(0, y1), min(height, y2)
        inside_area = sum(binary.crop((x1, y1, x2, y2)).histogram()[1:]) if x2 > x1 and y2 > y1 else 0
        box_coverage = inside_area / area
        if box_coverage < 0.5:
            warnings.append("Menos de la mitad de la máscara queda dentro de la caja marcada.")
    return {
        "area_pixels": area, "area_ratio": area_ratio, "positive_inside": positive_inside,
        "negative_inside_count": negative_inside_count, "connected_components": component_count,
        "box_coverage": box_coverage, "warnings": warnings,
    }


def accept_layer_mask_proposal(
    path: str | Path,
    layer_id: str,
    proposal_id: str,
    actor: str = "user",
) -> Path:
    project_path = Path(path).expanduser().resolve()
    data = load_project(project_path)
    layer = next((item for item in data.get("layers", []) if item.get("id") == layer_id), None)
    if layer is None:
        raise ProjectError(f"No existe la capa {layer_id}.")
    proposal = next((item for item in layer.get("mask_proposals", []) if item.get("id") == proposal_id), None)
    if proposal is None:
        raise ProjectError(f"No existe la propuesta {proposal_id}.")
    proposal_path = project_path.parent / proposal["mask"]
    try:
        with Image.open(proposal_path) as source_mask:
            mask = source_mask.convert("L")
    except OSError as exc:
        raise ProjectError(f"No pude leer la propuesta de máscara: {exc}") from exc
    image_size = tuple(data.get("image_size", ()))
    if image_size and mask.size != image_size:
        raise ProjectError("La propuesta no coincide con la resolución de la fuente.")
    quality = analyze_mask_proposal(mask, proposal)
    if not quality["positive_inside"]:
        raise ProjectError("La propuesta no cubre el punto positivo. Elige otra propuesta o vuelve a indicar la pieza.")
    if quality["negative_inside_count"]:
        raise ProjectError("La propuesta cubre un punto negativo. Elige otra propuesta o corrige las indicaciones.")
    relative = Path("assets") / "masks" / f"{layer_id}-accepted-{uuid.uuid4().hex[:8]}.png"
    mask_path = project_path.parent / relative
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    mask.save(mask_path, format="PNG")
    layer["mask"] = relative.as_posix()
    layer["segmentation"] = {
        "model": proposal.get("model"),
        "score": proposal.get("score"),
        "prompt": {
            "positive_points": proposal.get("positive_points", []),
            "negative_points": proposal.get("negative_points", []),
            "box": proposal.get("box"),
        },
        "mask_sha256": hashlib.sha256(mask_path.read_bytes()).hexdigest(),
        "accepted_at": now_utc(),
    }
    save_project(project_path, data, action=f"mask_proposal_accepted:{proposal_id}", actor=actor)
    return mask_path


def _layer_mask_path(project_path: Path, layer: dict[str, Any]) -> Path:
    path = Path(layer["mask"])
    return (path if path.is_absolute() else project_path.parent / path).resolve()


def _mask_signature(project_path: Path, layers: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for layer in layers:
        mask_path = _layer_mask_path(project_path, layer)
        if not mask_path.is_file():
            raise ProjectError(f"Falta la máscara de {layer.get('name', layer.get('id', 'capa'))}.")
        digest.update(str(layer.get("id", "")).encode("utf-8"))
        digest.update(mask_path.read_bytes())
    return digest.hexdigest()


def extract_layer(path: str | Path, layer_id: str, actor: str = "agent") -> Path:
    """Cut a layer from the preserved source using its editable grayscale mask."""
    project_path = Path(path).expanduser().resolve()
    data = load_project(project_path)
    layer = next((item for item in data.get("layers", []) if item.get("id") == layer_id), None)
    if layer is None:
        raise ProjectError(f"No existe la capa {layer_id}.")
    source_path = Path(data["_image_path"])
    mask_path = _layer_mask_path(project_path, layer)
    try:
        with Image.open(source_path) as source:
            rgba = source.convert("RGBA")
        with Image.open(mask_path) as source_mask:
            mask = source_mask.convert("L")
    except OSError as exc:
        raise ProjectError(f"No pude leer la fuente o la máscara: {exc}") from exc
    if rgba.size != mask.size:
        raise ProjectError("La máscara y la fuente tienen resoluciones distintas.")
    if mask.getbbox() is None:
        raise ProjectError("La máscara está vacía; píntala o importa una máscara antes de extraer la pieza.")
    alpha = ImageChops.multiply(rgba.getchannel("A"), mask)
    rgba.putalpha(alpha)
    relative = Path("assets") / "layers" / f"{layer_id}.png"
    sprite_path = project_path.parent / relative
    sprite_path.parent.mkdir(parents=True, exist_ok=True)
    rgba.save(sprite_path, format="PNG")
    layer["sprite"] = relative.as_posix()
    layer["separation"] = {
        "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "mask_sha256": hashlib.sha256(mask_path.read_bytes()).hexdigest(),
        "created_at": now_utc(),
    }
    save_project(project_path, data, action=f"layer_extracted:{layer_id}", actor=actor)
    return sprite_path


def inpaint_layer_holes(
    path: str | Path,
    radius: float = 3.0,
    method: str = "telea",
    actor: str = "agent",
) -> Path:
    """Reconstruct the source under all non-empty layer masks using OpenCV inpainting."""
    project_path = Path(path).expanduser().resolve()
    data = load_project(project_path)
    if radius <= 0 or radius > 100:
        raise ProjectError("El radio de inpainting debe estar entre 0 y 100 píxeles.")
    if method not in {"telea", "ns"}:
        raise ProjectError("Método no válido; usa 'telea' o 'ns'.")
    layers = data.get("layers", [])
    if not layers:
        raise ProjectError("Crea o importa máscaras antes de reconstruir las zonas ocultas.")
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise ProjectError("El inpainting necesita el extra OpenCV. Instala RigM con: pip install '.[inpaint]'") from exc
    source_path = Path(data["_image_path"])
    try:
        with Image.open(source_path) as source:
            original = source.convert("RGBA")
        union = Image.new("L", original.size, 0)
        active_ids = []
        for layer in layers:
            mask_path = _layer_mask_path(project_path, layer)
            with Image.open(mask_path) as source_mask:
                mask = source_mask.convert("L")
            if mask.size != original.size:
                raise ProjectError(f"La máscara de {layer.get('name', layer['id'])} no coincide con la fuente.")
            if mask.getbbox() is not None:
                union = ImageChops.lighter(union, mask)
                active_ids.append(layer["id"])
    except OSError as exc:
        raise ProjectError(f"No pude leer la fuente o las máscaras: {exc}") from exc
    if not active_ids:
        raise ProjectError("Todas las máscaras están vacías; no hay zonas que reconstruir.")
    for layer in layers:
        mask_path = _layer_mask_path(project_path, layer)
        if not mask_path.is_file():
            continue
        with Image.open(mask_path) as source_mask:
            non_empty = source_mask.convert("L").getbbox() is not None
        if not non_empty:
            continue
        separation = layer.get("separation", {})
        if not layer.get("sprite") or separation.get("mask_sha256") != hashlib.sha256(mask_path.read_bytes()).hexdigest():
            raise ProjectError(f"Extrae primero la pieza '{layer.get('name', layer['id'])}' con su máscara actual.")
    rgb = np.asarray(original.convert("RGB"))
    mask_array = np.asarray(union.point(lambda value: 255 if value >= 8 else 0), dtype=np.uint8)
    cv_method = cv2.INPAINT_TELEA if method == "telea" else cv2.INPAINT_NS
    reconstructed = cv2.inpaint(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), mask_array, float(radius), cv_method)
    result = Image.fromarray(cv2.cvtColor(reconstructed, cv2.COLOR_BGR2RGB)).convert("RGBA")
    result.putalpha(original.getchannel("A"))
    relative = Path("assets") / "reconstructions" / "base-inpaint.png"
    output_path = project_path.parent / relative
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(output_path, format="PNG")
    data["base_image"] = relative.as_posix()
    data["inpaint"] = {
        "method": method,
        "radius": float(radius),
        "layer_ids": active_ids,
        "mask_signature": _mask_signature(project_path, layers),
        "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "created_at": now_utc(),
        "review_required": True,
    }
    save_project(project_path, data, action="layers_inpainted", actor=actor)
    return output_path


def render_composite(path: str | Path) -> Image.Image:
    """Compose extracted sprites over the reconstructed base without changing the source."""
    project_path = Path(path).expanduser().resolve()
    data = load_project(project_path)
    source_path = Path(data["_image_path"])
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    layers = data.get("layers", [])
    base_value = data.get("base_image")
    base_path = source_path
    inpaint = data.get("inpaint", {})
    if base_value and inpaint.get("source_sha256") == source_hash:
        try:
            if inpaint.get("mask_signature") == _mask_signature(project_path, layers):
                base_path = project_path.parent / base_value
        except (OSError, ProjectError):
            pass
    try:
        with Image.open(base_path) as base:
            composite = base.convert("RGBA")
        for layer in layers:
            sprite_value = layer.get("sprite")
            if not sprite_value or not layer.get("visible", True):
                continue
            separation = layer.get("separation", {})
            try:
                current_mask_hash = hashlib.sha256(_layer_mask_path(project_path, layer).read_bytes()).hexdigest()
            except OSError:
                continue
            if separation.get("mask_sha256") != current_mask_hash or separation.get("source_sha256") != source_hash:
                continue
            sprite_path = project_path.parent / sprite_value
            with Image.open(sprite_path) as sprite:
                if sprite.size != composite.size:
                    raise ProjectError(f"La pieza {layer.get('name', layer['id'])} no coincide con la resolución de la escena.")
                composite.alpha_composite(sprite.convert("RGBA"))
        return composite
    except OSError as exc:
        raise ProjectError(f"No pude construir la composición: {exc}") from exc


def export_composite(path: str | Path, output: str | Path) -> Path:
    output_path = Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    render_composite(path).save(output_path)
    return output_path


def add_joint(path: str | Path, name: str, x: float, y: float) -> str:
    data = load_project(path)
    joint_id = f"joint-{uuid.uuid4().hex[:10]}"
    data["joints"].append({"id": joint_id, "name": name, "x": float(x), "y": float(y), "parent": None, "rotation": 0.0})
    data["stage"] = "skeleton"
    save_project(path, data, action="joint_added", actor="agent")
    return joint_id


def move_joint(path: str | Path, joint_id: str, x: float, y: float) -> None:
    data = load_project(path)
    joint = next((item for item in data["joints"] if item["id"] == joint_id), None)
    if joint is None:
        raise ProjectError(f"No existe el pivote {joint_id}.")
    joint["x"], joint["y"] = float(x), float(y)
    save_project(path, data, action="joint_moved", actor="agent")


def connect_joints(path: str | Path, start_id: str, end_id: str) -> None:
    data = load_project(path)
    known = {item["id"] for item in data["joints"]}
    if start_id == end_id:
        raise ProjectError("Un hueso no puede conectar un pivote consigo mismo.")
    if start_id not in known or end_id not in known:
        raise ProjectError("Los dos pivotes deben existir en el proyecto.")
    if any(set(edge) == {start_id, end_id} for edge in data["bones"]):
        raise ProjectError("Ese hueso ya existe.")
    child = next(item for item in data["joints"] if item["id"] == end_id)
    if child.get("parent") not in (None, start_id):
        raise ProjectError("El pivote final ya tiene otro padre. Desconéctalo antes de reasignarlo.")
    cursor = start_id
    while cursor:
        if cursor == end_id:
            raise ProjectError("Ese hueso crearía un ciclo en la jerarquía.")
        parent_joint = next((item for item in data["joints"] if item["id"] == cursor), None)
        cursor = parent_joint.get("parent") if parent_joint else None
    child["parent"] = start_id
    data["bones"].append([start_id, end_id])
    data["stage"] = "skeleton"
    save_project(path, data, action="bone_connected", actor="agent")


def set_joint_parent(path: str | Path, joint_id: str, parent_id: str | None, actor: str = "agent") -> None:
    data = load_project(path)
    by_id = {joint["id"]: joint for joint in data.get("joints", [])}
    if joint_id not in by_id or (parent_id is not None and parent_id not in by_id):
        raise ProjectError("La articulación o su padre no existe.")
    if parent_id == joint_id:
        raise ProjectError("Una articulación no puede ser su propio padre.")
    cursor = parent_id
    while cursor:
        if cursor == joint_id:
            raise ProjectError("Ese parentesco crearía un ciclo.")
        cursor = by_id[cursor].get("parent")
    old_parent = by_id[joint_id].get("parent")
    if old_parent:
        data["bones"] = [edge for edge in data.get("bones", []) if edge != [old_parent, joint_id]]
    by_id[joint_id]["parent"] = parent_id
    if parent_id:
        data["bones"] = [edge for edge in data.get("bones", []) if set(edge) != {joint_id, parent_id}]
        data["bones"].append([parent_id, joint_id])
    save_project(path, data, action="joint_parent_changed", actor=actor)


def create_animation_clip(
    path: str | Path,
    name: str,
    duration: int = 60,
    fps: int = 30,
    loop: bool = True,
    actor: str = "agent",
) -> str:
    if not name.strip() or duration < 1 or duration > 36000 or fps < 1 or fps > 120:
        raise ProjectError("Nombre, duración o FPS del clip no válidos.")
    data = load_project(path)
    clip_id = f"clip-{uuid.uuid4().hex[:12]}"
    data.setdefault("animations", []).append({
        "id": clip_id, "name": name.strip(), "duration": int(duration), "fps": int(fps),
        "loop": bool(loop), "tracks": {}, "created_at": now_utc(),
    })
    save_project(path, data, action="animation_created", actor=actor)
    return clip_id


def set_rotation_key(
    path: str | Path,
    clip_id: str,
    joint_id: str,
    frame: int,
    value: float,
    interpolation: str = "smooth",
    actor: str = "agent",
) -> None:
    if interpolation not in {"smooth", "linear"}:
        raise ProjectError("Interpolación no válida; usa 'smooth' o 'linear'.")
    data = load_project(path)
    clip = next((item for item in data.get("animations", []) if item.get("id") == clip_id), None)
    if clip is None:
        raise ProjectError(f"No existe el clip {clip_id}.")
    if not any(item.get("id") == joint_id for item in data.get("joints", [])):
        raise ProjectError(f"No existe la articulación {joint_id}.")
    if frame < 0 or frame > int(clip["duration"]):
        raise ProjectError("El fotograma queda fuera de la duración del clip.")
    track = clip.setdefault("tracks", {}).setdefault(joint_id, {}).setdefault("rotation", [])
    track[:] = [key for key in track if int(key["frame"]) != frame]
    track.append({"frame": int(frame), "value": float(value), "interpolation": interpolation})
    track.sort(key=lambda key: key["frame"])
    save_project(path, data, action=f"rotation_key_set:{clip_id}:{joint_id}:{frame}", actor=actor)


def create_expression(
    path: str | Path,
    name: str,
    joint_rotations: dict[str, float] | None = None,
    layer_opacities: dict[str, float] | None = None,
    actor: str = "agent",
) -> str:
    if not name.strip():
        raise ProjectError("La expresión necesita un nombre.")
    data = load_project(path)
    expression_id = f"expression-{uuid.uuid4().hex[:12]}"
    data.setdefault("expressions", []).append({
        "id": expression_id, "name": name.strip(),
        "joint_rotations": {key: float(value) for key, value in (joint_rotations or {}).items()},
        "layer_opacities": {key: float(value) for key, value in (layer_opacities or {}).items()},
        "created_at": now_utc(),
    })
    save_project(path, data, action="expression_created", actor=actor)
    return expression_id


def set_expression_value(
    path: str | Path,
    expression_id: str,
    target_id: str,
    value: float,
    target_type: str = "layer_opacity",
    actor: str = "agent",
) -> None:
    data = load_project(path)
    expression = next((item for item in data.get("expressions", []) if item.get("id") == expression_id), None)
    if expression is None:
        raise ProjectError(f"No existe la expresión {expression_id}.")
    if target_type == "layer_opacity":
        if not any(layer.get("id") == target_id for layer in data.get("layers", [])) or not 0 <= value <= 1:
            raise ProjectError("La capa no existe o la opacidad debe estar entre 0 y 1.")
        expression.setdefault("layer_opacities", {})[target_id] = float(value)
    elif target_type == "joint_rotation":
        if not any(joint.get("id") == target_id for joint in data.get("joints", [])):
            raise ProjectError(f"No existe la articulación {target_id}.")
        expression.setdefault("joint_rotations", {})[target_id] = float(value)
    else:
        raise ProjectError("Tipo de parámetro no válido.")
    save_project(path, data, action=f"expression_value_set:{expression_id}:{target_id}", actor=actor)


def update_workflow(
    path: str | Path,
    status: str,
    stage: str,
    detail: str = "",
    progress: int | None = None,
    task_id: str | None = None,
) -> None:
    allowed = {"in_progress", "queued", "needs_review", "ready", "failed"}
    if status not in allowed:
        raise ProjectError(f"Estado no válido: {status}.")
    if status == "ready":
        report = validate_project(path)
        if report["errors"] or report["warnings"]:
            reasons = report["errors"] + report["warnings"]
            raise ProjectError("No se puede marcar como listo: " + "; ".join(reasons))
    data = load_project(path)
    data["status"], data["stage"] = status, stage
    if progress is not None:
        if not 0 <= progress <= 100:
            raise ProjectError("El progreso debe estar entre 0 y 100.")
        data["progress"] = int(progress)
    elif status == "ready":
        data["progress"] = 100
    if task_id is not None:
        data["task_id"] = task_id
    if detail:
        data["last_result"] = detail
    save_project(path, data, action="workflow_updated", actor="agent")


def validate_project(path: str | Path) -> dict[str, Any]:
    project_path = Path(path).expanduser().resolve()
    data = load_project(project_path)
    errors: list[str] = []
    warnings: list[str] = []
    source_size = None
    try:
        image_path = resolve_image(data, project_path)
        if not image_path.is_file():
            errors.append(f"Falta la imagen de referencia: {image_path}")
        else:
            with Image.open(image_path) as image:
                source_size = image.size
    except (KeyError, OSError) as exc:
        errors.append(f"Referencia de imagen inválida: {exc}")

    decomposition_quality = data.get("decomposition", {}).get("quality", {})
    warnings.extend(str(item) for item in decomposition_quality.get("warnings", []))
    layers = data.get("layers", [])
    if not isinstance(layers, list):
        errors.append("La lista de capas tiene un formato inválido.")
        layers = []
    for layer in layers:
        if not isinstance(layer, dict) or not layer.get("id") or not layer.get("mask"):
            errors.append("Hay una capa sin identificador o sin máscara.")
            continue
        layer_mask = Path(layer["mask"])
        if not layer_mask.is_absolute():
            layer_mask = project_path.parent / layer_mask
        if not layer_mask.is_file():
            errors.append(f"Falta la máscara de la capa {layer.get('name', layer['id'])}.")
            continue
        try:
            with Image.open(layer_mask) as mask:
                if source_size and mask.size != source_size:
                    errors.append(f"La máscara de {layer.get('name', layer['id'])} no coincide con la resolución de la fuente.")
                if mask.convert("L").getbbox() is None:
                    warnings.append(f"La máscara de {layer.get('name', layer['id'])} está vacía.")
        except OSError as exc:
            errors.append(f"No pude leer la máscara de {layer.get('name', layer['id'])}: {exc}")
        sprite_value = layer.get("sprite")
        if not sprite_value:
            warnings.append(f"La capa {layer.get('name', layer['id'])} aún no se extrajo como pieza.")
        else:
            sprite_path = project_path.parent / sprite_value
            if not sprite_path.is_file():
                errors.append(f"Falta la pieza extraída de {layer.get('name', layer['id'])}.")
            else:
                separation = layer.get("separation", {})
                source_hash = hashlib.sha256(Path(data["_image_path"]).read_bytes()).hexdigest() if Path(data["_image_path"]).is_file() else ""
                if layer_mask.is_file() and (
                    separation.get("mask_sha256") != hashlib.sha256(layer_mask.read_bytes()).hexdigest()
                    or separation.get("source_sha256") != source_hash
                ):
                    warnings.append(f"La pieza de {layer.get('name', layer['id'])} está desactualizada; extráela de nuevo.")
        segmentation = layer.get("segmentation", {})
        if segmentation.get("mask_sha256") and layer_mask.is_file():
            if segmentation["mask_sha256"] != hashlib.sha256(layer_mask.read_bytes()).hexdigest():
                warnings.append(f"La propuesta aceptada de {layer.get('name', layer['id'])} ya no coincide con su máscara editable.")
        proposals = layer.get("mask_proposals", [])
        if not isinstance(proposals, list):
            errors.append(f"Las propuestas de {layer.get('name', layer['id'])} tienen un formato inválido.")
            proposals = []
        for proposal in proposals:
            if not isinstance(proposal, dict) or not proposal.get("id") or not proposal.get("mask"):
                errors.append(f"Hay una propuesta incompleta en {layer.get('name', layer['id'])}.")
                continue
            proposal_path = project_path.parent / proposal["mask"]
            if not proposal_path.is_file():
                errors.append(f"Falta una propuesta de máscara de {layer.get('name', layer['id'])}.")
                continue
            try:
                with Image.open(proposal_path) as proposal_image:
                    if source_size and proposal_image.size != source_size:
                        errors.append(f"Una propuesta de {layer.get('name', layer['id'])} no coincide con la fuente.")
            except OSError as exc:
                errors.append(f"No pude leer una propuesta de {layer.get('name', layer['id'])}: {exc}")
    base_value = data.get("base_image")
    if base_value:
        base_path = project_path.parent / base_value
        if not base_path.is_file():
            errors.append("Falta la imagen base reconstruida.")
        else:
            try:
                with Image.open(base_path) as base_image:
                    if source_size and base_image.size != source_size:
                        errors.append("La imagen base reconstruida no coincide con la resolución de la fuente.")
                inpaint = data.get("inpaint", {})
                if layers and inpaint.get("mask_signature") != _mask_signature(project_path, layers):
                    warnings.append("La reconstrucción está desactualizada; vuelve a rellenar las zonas ocultas.")
                if inpaint.get("source_sha256") and inpaint["source_sha256"] != hashlib.sha256(Path(data["_image_path"]).read_bytes()).hexdigest():
                    warnings.append("La reconstrucción se creó con otra versión de la imagen fuente.")
            except (OSError, ProjectError) as exc:
                errors.append(f"No pude validar la imagen reconstruida: {exc}")

    joints = data.get("joints", [])
    bones = data.get("bones", [])
    ids = [item.get("id") for item in joints]
    if len(ids) != len(set(ids)):
        errors.append("Hay identificadores de pivote repetidos.")
    known = set(ids)
    adjacency = {joint_id: set() for joint_id in known}
    for edge in bones:
        if not isinstance(edge, list) or len(edge) != 2:
            errors.append("Hay una conexión con formato inválido.")
            continue
        a, b = edge
        if a == b:
            errors.append("Hay un hueso conectado a sí mismo.")
        elif a not in known or b not in known:
            errors.append(f"Un hueso apunta a un pivote inexistente: {a}, {b}.")
        else:
            adjacency[a].add(b)
            adjacency[b].add(a)
    visited: set[str] = set()
    for start in adjacency:
        if start in visited:
            continue
        stack = [(start, None)]
        while stack:
            node, parent = stack.pop()
            if node in visited:
                errors.append("El esqueleto contiene un ciclo.")
                break
            visited.add(node)
            stack.extend((neighbor, node) for neighbor in adjacency[node] if neighbor != parent)
    if not joints:
        warnings.append("El proyecto aún no tiene pivotes.")
    if joints and not bones:
        warnings.append("El proyecto tiene pivotes, pero ningún hueso conectado.")
    mesh_count = 0
    for layer in layers:
        mesh = layer.get("mesh")
        if not mesh:
            if layer.get("sprite"):
                warnings.append(f"Falta generar la malla deformable de {layer.get('name', layer['id'])}.")
            continue
        mesh_count += 1
        points, triangles, weights = mesh.get("points", []), mesh.get("triangles", []), mesh.get("weights", [])
        if len(points) != len(weights) or not points or not triangles:
            errors.append(f"La malla de {layer.get('name', layer['id'])} tiene una estructura incompleta.")
        for vertex_index, vertex_weights in enumerate(weights):
            if not vertex_weights or any(joint_id not in known for joint_id in vertex_weights):
                errors.append(f"Los pesos del vértice {vertex_index} de {layer.get('name', layer['id'])} tienen articulaciones inválidas.")
                break
            if abs(sum(float(value) for value in vertex_weights.values()) - 1.0) > 1e-3:
                errors.append(f"Los pesos del vértice {vertex_index} de {layer.get('name', layer['id'])} no suman 1.")
                break
        if any(not isinstance(triangle, list) or len(triangle) != 3 or any(not isinstance(index, int) or index < 0 or index >= len(points) for index in triangle) for triangle in triangles):
            errors.append(f"La malla de {layer.get('name', layer['id'])} contiene triángulos inválidos.")
        if mesh.get("skeleton_sha256") and mesh["skeleton_sha256"] != skeleton_signature(data):
            warnings.append(f"La malla de {layer.get('name', layer['id'])} está obsoleta tras cambiar el esqueleto.")
        if layer.get("sprite") and layer.get("separation", {}).get("mask_sha256") != mesh.get("mask_sha256"):
            warnings.append(f"La malla de {layer.get('name', layer['id'])} está obsoleta tras cambiar su máscara.")
    if layers and mesh_count == 0:
        warnings.append("Falta configurar mallas deformables y pesos.")
    expressions = data.get("expressions", [])
    animations = data.get("animations", [])
    if not expressions:
        warnings.append("Faltan expresiones faciales.")
    if not animations:
        warnings.append("Faltan clips de animación.")
    for clip in animations:
        for joint_id, track in clip.get("tracks", {}).items():
            if joint_id not in known:
                errors.append(f"El clip {clip.get('name', clip.get('id'))} apunta a una articulación inexistente.")
            frames = [int(key.get("frame", -1)) for key in track.get("rotation", [])]
            if any(frame < 0 or frame > int(clip.get("duration", 0)) for frame in frames):
                errors.append(f"El clip {clip.get('name', clip.get('id'))} tiene claves fuera de su duración.")
    for expression in expressions:
        if any(joint_id not in known for joint_id in expression.get("joint_rotations", {})):
            errors.append(f"La expresión {expression.get('name', expression.get('id'))} apunta a una articulación inexistente.")
        layer_ids = {layer.get("id") for layer in layers}
        if any(layer_id not in layer_ids for layer_id in expression.get("layer_opacities", {})):
            errors.append(f"La expresión {expression.get('name', expression.get('id'))} apunta a una capa inexistente.")
    if not layers:
        warnings.append("Falta separar las capas del personaje.")
    return {
        "project": str(project_path),
        "name": data.get("name", project_path.stem),
        "status": "failed" if errors else ("needs_review" if warnings else "ready"),
        "errors": errors,
        "warnings": warnings,
        "joint_count": len(joints),
        "bone_count": len(bones),
        "layer_count": len(layers),
        "mesh_count": mesh_count,
        "animation_count": len(animations),
        "expression_count": len(expressions),
    }


def import_decomposition_psd(
    path: str | Path,
    psd_path: str | Path,
    padded_size: tuple[int, int],
    source_size: tuple[int, int],
    offset: tuple[int, int],
    actor: str = "user",
    group_layers: bool = True,
) -> int:
    """Import semantic RGBA layers from See-through's PSD into the current rig."""
    try:
        from psd_tools import PSDImage
    except ImportError as exc:
        raise ProjectError("La importación PSD requiere psd-tools. Instala RigM con: pip install -e '.[decomposition]'") from exc
    project_path = Path(path).expanduser().resolve()
    source_psd = Path(psd_path).expanduser().resolve()
    data = load_project(project_path)
    try:
        psd = PSDImage.open(source_psd)
        psd_size = tuple(psd.size)
        if psd_size[0] < 1 or psd_size[1] < 1:
            raise ProjectError("El PSD de See-through no tiene un lienzo válido.")
        resize = Image.Resampling.LANCZOS
        imported: list[tuple[str, Image.Image]] = []
        ignored = {"src_img", "src_head", "reconstruction", "source", "reference", "original"}
        for layer in psd.descendants():
            if layer.is_group() or not layer.is_visible():
                continue
            name = str(layer.name or "Pieza IA").strip()
            normalized_name = name.lower().replace(" ", "_")
            if normalized_name in ignored or normalized_name.endswith("_depth") or "depth_map" in normalized_name:
                continue
            piece = layer.composite()
            if piece is None:
                continue
            piece = piece.convert("RGBA")
            left, top, right, bottom = (int(value) for value in layer.bbox)
            expected_piece_size = (max(0, right - left), max(0, bottom - top))
            if expected_piece_size[0] == 0 or expected_piece_size[1] == 0:
                continue
            if piece.size != expected_piece_size:
                piece = piece.resize(expected_piece_size, resize)
            canvas = Image.new("RGBA", psd_size, (0, 0, 0, 0))
            x0, y0 = max(0, left), max(0, top)
            x1, y1 = min(psd_size[0], right), min(psd_size[1], bottom)
            if x1 <= x0 or y1 <= y0:
                continue
            visible_piece = piece.crop((x0 - left, y0 - top, x1 - left, y1 - top))
            canvas.alpha_composite(visible_piece, (x0, y0))
            if canvas.size != padded_size:
                canvas = canvas.resize(padded_size, resize)
            crop_left, crop_top = offset
            width, height = source_size
            canvas = canvas.crop((crop_left, crop_top, crop_left + width, crop_top + height))
            alpha = canvas.getchannel("A")
            if alpha.getbbox() is None:
                continue
            if alpha.getbbox() == (0, 0, width, height) and alpha.getextrema() == (255, 255):
                continue
            imported.append((name, canvas))
    except ProjectError:
        raise
    except Exception as exc:
        raise ProjectError(f"No pude leer las capas del PSD: {exc}") from exc
    if not imported:
        raise ProjectError("El PSD no contiene capas semánticas visibles que RigM pueda importar.")

    # Detectar resultados sospechosos sin descartar capas automáticamente: un
    # modelo puede producir piezas pequeñas válidas, pero merece revisión visual.
    source_pixels = max(1, source_size[0] * source_size[1])
    layer_masks = [sprite.getchannel("A") for _, sprite in imported]
    layer_areas = [sum(index * count for index, count in enumerate(mask.histogram())) / 255 / source_pixels for mask in layer_masks]
    quality_warnings: list[str] = []
    if len(imported) < 3:
        quality_warnings.append("La separación generó menos de 3 capas; puede haber piezas del personaje aún unidas.")
    if max(layer_areas, default=0) >= 0.75:
        largest_name = imported[layer_areas.index(max(layer_areas))][0]
        quality_warnings.append(f"La capa «{largest_name}» cubre al menos el 75 % de la imagen; revisa si contiene varias piezas unidas.")
    tiny_count = sum(area < 0.001 for area in layer_areas)
    if tiny_count >= 5:
        quality_warnings.append(f"Hay {tiny_count} capas muy pequeñas; podrían ser fragmentos o ruido de segmentación.")
    duplicate_count = 0
    for left_index, left_mask in enumerate(layer_masks):
        left_hist = left_mask.histogram()
        left_area = sum(index * count for index, count in enumerate(left_hist))
        if not left_area:
            continue
        for right_index in range(left_index + 1, len(layer_masks)):
            right_mask = layer_masks[right_index]
            intersection = ImageChops.multiply(left_mask, right_mask)
            intersection_area = sum(index * count for index, count in enumerate(intersection.histogram()))
            right_area = sum(index * count for index, count in enumerate(right_mask.histogram()))
            union = left_area + right_area - intersection_area
            if union and intersection_area / union >= 0.97:
                duplicate_count += 1
    if duplicate_count:
        quality_warnings.append(f"Se detectaron {duplicate_count} pares de capas casi duplicadas; revisa posibles solapamientos.")

    output_dir = project_path.parent / "assets" / "decompositions"
    output_dir.mkdir(parents=True, exist_ok=True)
    mask_dir = project_path.parent / "assets" / "masks"
    sprite_dir = project_path.parent / "assets" / "sprites"
    mask_dir.mkdir(parents=True, exist_ok=True)
    sprite_dir.mkdir(parents=True, exist_ok=True)
    source_image = Path(data["_image_path"])
    source_hash = hashlib.sha256(source_image.read_bytes()).hexdigest()
    prior = [layer for layer in data.get("layers", []) if layer.get("decomposition", {}).get("engine") == "see-through"]
    if prior:
        data.setdefault("decomposition_history", []).append({
            "engine": "see-through", "replaced_layer_ids": [layer["id"] for layer in prior],
            "replaced_at": now_utc(), "layers": prior,
        })
        data["layers"] = [layer for layer in data.get("layers", []) if layer not in prior]
    base_order = max((int(layer.get("draw_order", index)) for index, layer in enumerate(data.get("layers", []))), default=-1) + len(imported)
    psd_relative = source_psd.relative_to(project_path.parent).as_posix()
    added_at = now_utc()
    group_id = f"group-ai-{uuid.uuid4().hex[:12]}" if group_layers else None
    group_name = "Separación IA · See-through"
    for index, (name, sprite) in enumerate(imported):
        layer_id = f"layer-{uuid.uuid4().hex[:12]}"
        mask_relative = Path("assets") / "masks" / f"{layer_id}.png"
        sprite_relative = Path("assets") / "sprites" / f"{layer_id}.png"
        mask_path, sprite_path = project_path.parent / mask_relative, project_path.parent / sprite_relative
        mask = sprite.getchannel("A")
        mask.save(mask_path, format="PNG")
        sprite.save(sprite_path, format="PNG")
        mask_hash = hashlib.sha256(mask_path.read_bytes()).hexdigest()
        data.setdefault("layers", []).append({
            "id": layer_id, "name": name, "mask": mask_relative.as_posix(),
            "sprite": sprite_relative.as_posix(), "visible": True,
            "draw_order": base_order - index,
            "separation": {
                "source_sha256": source_hash, "mask_sha256": mask_hash,
                "method": "see-through", "created_at": added_at,
            },
            "decomposition": {"engine": "see-through", "psd": psd_relative, "created_at": added_at},
            **({"group_id": group_id, "group_name": group_name} if group_id else {}),
        })
    data["decomposition"] = {
        "engine": "See-through", "psd": psd_relative, "layer_count": len(imported),
        "created_at": added_at, "review_required": True,
        "group_id": group_id, "group_name": group_name if group_id else None,
        "quality": {"warnings": quality_warnings, "layer_area_ratios": {name: round(area, 6) for (name, _), area in zip(imported, layer_areas)}},
    }
    data["stage"] = "layers"
    data["status"] = "needs_review"
    save_project(project_path, data, action="see_through_decomposition_imported", actor=actor)
    return len(imported)


class ProjectLibrary:
    """Persistent catalog that never mistakes a missing file for an existing rig."""

    def __init__(
        self,
        root: str | Path | None = None,
        projects_root: str | Path | None = None,
    ) -> None:
        app_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "RigM"
        self.root = Path(root or app_data).expanduser()
        self.projects_root = Path(projects_root or (Path.home() / "RigM/Projects")).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "library.json"

    def _read_index(self) -> list[str]:
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            return list(dict.fromkeys(str(Path(item).expanduser().resolve()) for item in raw.get("projects", [])))
        except (OSError, json.JSONDecodeError, AttributeError):
            return []

    def _write_index(self, paths: list[str]) -> None:
        payload = json.dumps({"version": 1, "projects": list(dict.fromkeys(paths))}, indent=2) + "\n"
        self.index_path.write_text(payload, encoding="utf-8")

    def add(self, path: str | Path) -> dict[str, Any]:
        project_path = Path(path).expanduser().resolve()
        data = _read_json(project_path)
        paths = self._read_index()
        key = str(project_path)
        if key not in paths:
            paths.append(key)
        self._write_index(paths)
        return self._entry(project_path, data)

    def list_projects(self) -> list[dict[str, Any]]:
        paths = self._read_index()
        changed = False
        if self.projects_root.is_dir():
            for project_path in self.projects_root.glob("*.rigm"):
                key = str(project_path.resolve())
                if key not in paths:
                    paths.append(key)
                    changed = True
        if changed:
            self._write_index(paths)
        entries = []
        for value in paths:
            path = Path(value)
            if not path.is_file():
                entries.append({"path": value, "name": path.stem, "status": "missing", "stage": "unknown", "updated_at": ""})
                continue
            try:
                entries.append(self._entry(path, _read_json(path)))
            except ProjectError as exc:
                entries.append({"path": value, "name": path.stem, "status": "invalid", "stage": str(exc), "updated_at": ""})
        return sorted(entries, key=lambda item: (item.get("updated_at", ""), item["name"].lower()), reverse=True)

    def remove(self, path: str | Path) -> None:
        key = str(Path(path).expanduser().resolve())
        self._write_index([item for item in self._read_index() if item != key])

    @staticmethod
    def _entry(path: Path, data: dict[str, Any]) -> dict[str, Any]:
        image_path = resolve_image(data, path) if "image" in data else None
        status = data.get("status", "in_progress")
        if image_path is not None and not image_path.is_file():
            status = "missing_source"
        return {
            "id": data.get("id", str(path)),
            "path": str(path),
            "name": data.get("name", path.stem),
            "status": status,
            "stage": data.get("stage", "skeleton"),
            "progress": data.get("progress", 0),
            "layer_count": len(data.get("layers", [])),
            "task_id": data.get("task_id", ""),
            "updated_at": data.get("updated_at", ""),
            "image": str(image_path) if image_path else "",
            "last_result": data.get("last_result", ""),
        }
