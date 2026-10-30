"""Mesh binding, 2D skeletal deformation, animation sampling, and pose rendering."""

from __future__ import annotations

import math
import hashlib
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .project import ProjectError, _layer_mask_path, load_project, now_utc, save_project, skeleton_signature


_RENDER_RESOURCE_CACHE: tuple[tuple[tuple[str, int | None, int | None], ...], Image.Image, dict[str, Image.Image]] | None = None


def _resource_signature(paths: list[Path]) -> tuple[tuple[str, int | None, int | None], ...]:
    signature = []
    for path in paths:
        try:
            stat = path.stat()
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            signature.append((str(path), None, None))
    return tuple(signature)


def _render_resources(project_path: Path, data: dict[str, Any]) -> tuple[Image.Image, dict[str, Image.Image]]:
    """Load and validate static render inputs once while a project is unchanged."""
    global _RENDER_RESOURCE_CACHE
    base_source = Path(data["_image_path"])
    base_value = data.get("base_image")
    resources = [project_path, base_source]
    if base_value:
        resources.append(project_path.parent / base_value)
    for layer in data.get("layers", []):
        if layer.get("sprite"):
            resources.append(project_path.parent / layer["sprite"])
            resources.append(_layer_mask_path(project_path, layer))
    signature = _resource_signature(resources)
    if _RENDER_RESOURCE_CACHE and _RENDER_RESOURCE_CACHE[0] == signature:
        _, base, sprites = _RENDER_RESOURCE_CACHE
        return base.copy(), sprites

    base_path = base_source
    if base_value and data.get("inpaint", {}).get("source_sha256"):
        try:
            from .project import _mask_signature
            source_hash = hashlib.sha256(base_source.read_bytes()).hexdigest()
            if (data["inpaint"].get("source_sha256") == source_hash
                    and data["inpaint"].get("mask_signature") == _mask_signature(project_path, data.get("layers", []))):
                base_path = project_path.parent / base_value
        except (OSError, ProjectError):
            pass
    try:
        with Image.open(base_path) as opened_base:
            base = opened_base.convert("RGBA")
        if not (base_value and base_path != base_source):
            masks = []
            for layer in data.get("layers", []):
                if not layer.get("sprite"):
                    continue
                mask_path = _layer_mask_path(project_path, layer)
                if mask_path.is_file():
                    with Image.open(mask_path) as opened_mask:
                        masks.append(opened_mask.convert("L"))
            if masks and any(mask.getbbox() for mask in masks):
                cv2, np = _vision_dependencies()
                union = np.maximum.reduce([np.asarray(mask, dtype=np.uint8) for mask in masks])
                rgba = np.asarray(base, dtype=np.uint8)
                repaired = cv2.inpaint(cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2BGR),
                                       np.where(union >= 8, 255, 0).astype(np.uint8), 3,
                                       cv2.INPAINT_TELEA)
                clean = Image.fromarray(cv2.cvtColor(repaired, cv2.COLOR_BGR2RGB)).convert("RGBA")
                clean.putalpha(base.getchannel("A"))
                base = clean

        expected_skeleton = skeleton_signature(data)
        sprites: dict[str, Image.Image] = {}
        for layer in data.get("layers", []):
            if not layer.get("sprite"):
                continue
            sprite_path = project_path.parent / layer["sprite"]
            mask_path = _layer_mask_path(project_path, layer)
            if layer.get("mesh") and (
                layer["mesh"].get("sprite_sha256") != hashlib.sha256(sprite_path.read_bytes()).hexdigest()
                or layer["mesh"].get("mask_sha256") != hashlib.sha256(mask_path.read_bytes()).hexdigest()
                or layer["mesh"].get("skeleton_sha256") != expected_skeleton
            ):
                raise ProjectError(f"La malla de {layer.get('name', layer['id'])} está desactualizada. Vuelve a extraer la pieza y generar su malla.")
            with Image.open(sprite_path) as opened_sprite:
                sprites[layer["id"]] = opened_sprite.convert("RGBA")
    except OSError as exc:
        raise ProjectError(f"No pude renderizar la pose: {exc}") from exc
    _RENDER_RESOURCE_CACHE = (signature, base.copy(), sprites)
    return base, sprites


def _vision_dependencies():
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise ProjectError("La malla deformable requiere OpenCV. Instala RigM con: pip install -e '.[rigging]'") from exc
    return cv2, np


def generate_layer_mesh(
    path: str | Path,
    layer_id: str,
    spacing: int = 28,
    actor: str = "agent",
    bind_joint_id: str | None = None,
) -> dict[str, Any]:
    """Create a regular 2D triangle grid and normalized nearest-joint skin weights."""
    project_path = Path(path).expanduser().resolve()
    data = load_project(project_path)
    layer = next((item for item in data.get("layers", []) if item.get("id") == layer_id), None)
    if layer is None:
        raise ProjectError(f"No existe la capa {layer_id}.")
    if not layer.get("sprite"):
        raise ProjectError("Extrae la máscara como pieza antes de generar una malla.")
    joints = data.get("joints", [])
    if not joints:
        raise ProjectError("Añade articulaciones antes de generar una malla deformable.")
    if bind_joint_id and not any(str(joint["id"]) == bind_joint_id for joint in joints):
        raise ProjectError(f"No existe la articulación {bind_joint_id}.")
    if spacing < 4 or spacing > 256:
        raise ProjectError("La separación de vértices debe estar entre 4 y 256 píxeles.")
    sprite_path = project_path.parent / layer["sprite"]
    try:
        with Image.open(sprite_path) as sprite:
            alpha = sprite.convert("RGBA").getchannel("A")
    except OSError as exc:
        raise ProjectError(f"No pude leer la pieza: {exc}") from exc
    bounds = alpha.getbbox()
    if bounds is None:
        raise ProjectError("La pieza no tiene píxeles visibles.")
    left, top, right, bottom = bounds
    width, height = right - left, bottom - top
    if width < 2 or height < 2:
        raise ProjectError("La pieza es demasiado pequeña para crear una malla.")
    columns = max(2, math.ceil(width / spacing) + 1)
    rows = max(2, math.ceil(height / spacing) + 1)
    points = [
        [left + width * column / (columns - 1), top + height * row / (rows - 1)]
        for row in range(rows)
        for column in range(columns)
    ]
    triangles: list[list[int]] = []
    for row in range(rows - 1):
        for column in range(columns - 1):
            top_left = row * columns + column
            top_right = top_left + 1
            bottom_left = top_left + columns
            bottom_right = bottom_left + 1
            if (row + column) % 2:
                triangles.extend(([top_left, top_right, bottom_left], [top_right, bottom_right, bottom_left]))
            else:
                triangles.extend(([top_left, top_right, bottom_right], [top_left, bottom_right, bottom_left]))
    joint_positions = [(str(joint["id"]), float(joint["x"]), float(joint["y"])) for joint in joints]
    weights = []
    for x, y in points:
        if bind_joint_id:
            weights.append({bind_joint_id: 1.0})
            continue
        distances = sorted(
            ((math.hypot(x - jx, y - jy), joint_id) for joint_id, jx, jy in joint_positions)
        )[: min(4, len(joint_positions))]
        if distances[0][0] < 1e-6:
            weights.append({distances[0][1]: 1.0})
            continue
        raw = [(joint_id, 1.0 / max(distance, spacing * 0.15) ** 2) for distance, joint_id in distances]
        total = sum(weight for _, weight in raw)
        weights.append({joint_id: weight / total for joint_id, weight in raw})
    mesh = {
        "version": 1,
        "points": points,
        "triangles": triangles,
        "weights": weights,
        "spacing": spacing,
        "source_size": list(alpha.size),
        "sprite": layer["sprite"],
        "sprite_sha256": hashlib.sha256(sprite_path.read_bytes()).hexdigest(),
        "mask_sha256": layer.get("separation", {}).get("mask_sha256"),
        "skeleton_sha256": skeleton_signature(data),
        "created_at": now_utc(),
    }
    layer["mesh"] = mesh
    save_project(project_path, data, action=f"mesh_generated:{layer_id}", actor=actor)
    return mesh


def _transform_matrices(joints: list[dict[str, Any]], rotations: dict[str, float], np):
    by_id = {str(joint["id"]): joint for joint in joints}
    worlds: dict[str, Any] = {}
    visiting: set[str] = set()

    def translation(x: float, y: float):
        return np.array([[1.0, 0.0, x], [0.0, 1.0, y], [0.0, 0.0, 1.0]])

    def rotation(degrees: float):
        angle = math.radians(degrees)
        cosine, sine = math.cos(angle), math.sin(angle)
        return np.array([[cosine, -sine, 0.0], [sine, cosine, 0.0], [0.0, 0.0, 1.0]])

    def world(joint_id: str):
        if joint_id in worlds:
            return worlds[joint_id]
        if joint_id in visiting:
            raise ProjectError("La jerarquía de articulaciones contiene un ciclo.")
        joint = by_id[joint_id]
        visiting.add(joint_id)
        parent_id = joint.get("parent")
        angle = float(rotations.get(joint_id, 0.0))
        if parent_id and parent_id in by_id:
            parent = by_id[parent_id]
            matrix = world(parent_id) @ translation(float(joint["x"]) - float(parent["x"]), float(joint["y"]) - float(parent["y"])) @ rotation(angle)
        else:
            matrix = translation(float(joint["x"]), float(joint["y"])) @ rotation(angle)
        visiting.remove(joint_id)
        worlds[joint_id] = matrix
        return matrix

    deltas = {}
    for joint_id, joint in by_id.items():
        bind = translation(float(joint["x"]), float(joint["y"]))
        deltas[joint_id] = world(joint_id) @ np.linalg.inv(bind)
    return deltas


def posed_joint_positions(
    joints: list[dict[str, Any]], rotations: dict[str, float]
) -> dict[str, tuple[float, float]]:
    """Return forward-kinematic joint positions without optional vision deps."""
    by_id = {str(joint["id"]): joint for joint in joints}
    positions: dict[str, tuple[float, float]] = {}
    world_angles: dict[str, float] = {}
    visiting: set[str] = set()

    def solve(joint_id: str) -> tuple[float, float]:
        if joint_id in positions:
            return positions[joint_id]
        if joint_id in visiting:
            raise ProjectError("La jerarquía de articulaciones contiene un ciclo.")
        visiting.add(joint_id)
        joint = by_id[joint_id]
        parent_id = joint.get("parent")
        if parent_id and parent_id in by_id:
            parent = by_id[parent_id]
            px, py = solve(parent_id)
            angle = world_angles[parent_id]
            dx = float(joint["x"]) - float(parent["x"])
            dy = float(joint["y"]) - float(parent["y"])
            radians = math.radians(angle)
            x = px + dx * math.cos(radians) - dy * math.sin(radians)
            y = py + dx * math.sin(radians) + dy * math.cos(radians)
            world_angles[joint_id] = angle + float(rotations.get(joint_id, 0.0))
        else:
            x, y = float(joint["x"]), float(joint["y"])
            world_angles[joint_id] = float(rotations.get(joint_id, 0.0))
        positions[joint_id] = (x, y)
        visiting.remove(joint_id)
        return positions[joint_id]

    for joint_id in by_id:
        solve(joint_id)
    return positions


def deform_layer_sprite(
    sprite: Image.Image,
    mesh: dict[str, Any],
    joints: list[dict[str, Any]],
    rotations: dict[str, float],
) -> Image.Image:
    weights = mesh["weights"]
    known_joint_ids = {str(joint["id"]) for joint in joints}
    rigid_joint_id: str | None = None
    rigid_binding = bool(weights)
    for vertex_weights in weights:
        if len(vertex_weights) != 1:
            rigid_binding = False
            break
        joint_id, weight = next(iter(vertex_weights.items()))
        if abs(float(weight) - 1.0) > 1e-6 or joint_id not in known_joint_ids:
            rigid_binding = False
            break
        if rigid_joint_id is None:
            rigid_joint_id = joint_id
        elif rigid_joint_id != joint_id:
            rigid_binding = False
            break
    if rigid_binding and rigid_joint_id:
        by_id = {str(joint["id"]): joint for joint in joints}
        ancestor_id: str | None = rigid_joint_id
        while ancestor_id and ancestor_id in by_id:
            if abs(float(rotations.get(ancestor_id, 0.0))) > 1e-9:
                break
            ancestor_id = by_id[ancestor_id].get("parent")
        else:
            return sprite

    cv2, np = _vision_dependencies()
    points = np.asarray(mesh["points"], dtype=np.float64)
    if rigid_binding and rigid_joint_id:
        # Rigid sprites need only their small occupied region transformed; warping
        # the full canvas wastes work and triangle-by-triangle warps leave seams.
        width, height = sprite.size
        pad = 2
        source_left = max(0, int(math.floor(float(points[:, 0].min()))) - pad)
        source_top = max(0, int(math.floor(float(points[:, 1].min()))) - pad)
        source_right = min(width, int(math.ceil(float(points[:, 0].max()))) + pad)
        source_bottom = min(height, int(math.ceil(float(points[:, 1].max()))) + pad)
        if source_right <= source_left or source_bottom <= source_top:
            return Image.new("RGBA", (width, height))
        source_crop = np.asarray(sprite.crop(
            (source_left, source_top, source_right, source_bottom)
        ).convert("RGBA"))
        deltas = _transform_matrices(joints, rotations, np)
        transform = deltas[rigid_joint_id][:2, :].astype(np.float32)
        corners = np.array([
            [source_left, source_top, 1.0], [source_right, source_top, 1.0],
            [source_right, source_bottom, 1.0], [source_left, source_bottom, 1.0],
        ], dtype=np.float32)
        destination_corners = corners @ transform.T
        target_left = max(0, int(math.floor(float(destination_corners[:, 0].min()))) - pad)
        target_top = max(0, int(math.floor(float(destination_corners[:, 1].min()))) - pad)
        target_right = min(width, int(math.ceil(float(destination_corners[:, 0].max()))) + pad)
        target_bottom = min(height, int(math.ceil(float(destination_corners[:, 1].max()))) + pad)
        if target_right <= target_left or target_bottom <= target_top:
            return Image.new("RGBA", (width, height))
        alpha = source_crop[..., 3:4].astype(np.float32) / 255.0
        premultiplied = np.concatenate((source_crop[..., :3].astype(np.float32) * alpha,
                                        source_crop[..., 3:4].astype(np.float32)), axis=2)
        local_transform = transform.copy()
        local_transform[:, 2] += (
            transform[:, :2] @ np.array([source_left, source_top], dtype=np.float32)
            - np.array([target_left, target_top], dtype=np.float32)
        )
        warped = cv2.warpAffine(
            premultiplied, local_transform, (target_right - target_left, target_bottom - target_top), flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0),
        )
        warped_alpha = warped[..., 3:4] / 255.0
        rgb = np.zeros_like(warped[..., :3])
        visible = warped_alpha[..., 0] > 1e-6
        rgb[visible] = warped[..., :3][visible] / warped_alpha[visible]
        result = np.concatenate((np.clip(rgb, 0, 255),
                                 np.clip(warped[..., 3:4], 0, 255)), axis=2)
        output = Image.new("RGBA", (width, height))
        output.paste(Image.fromarray(result.astype(np.uint8), mode="RGBA"), (target_left, target_top))
        return output
    source = np.asarray(sprite.convert("RGBA"))
    height, width = source.shape[:2]
    deltas = _transform_matrices(joints, rotations, np)
    deformed = []
    for point, vertex_weights in zip(points, weights):
        homogeneous = np.array([point[0], point[1], 1.0])
        result = np.zeros(3, dtype=np.float64)
        total = 0.0
        for joint_id, weight in vertex_weights.items():
            if joint_id in deltas:
                result += float(weight) * (deltas[joint_id] @ homogeneous)
                total += float(weight)
        deformed.append(result[:2] / total if total else point)
    deformed = np.asarray(deformed, dtype=np.float32)
    output = np.zeros_like(source)
    for triangle in mesh["triangles"]:
        indices = np.asarray(triangle, dtype=np.int32)
        src_tri = points[indices].astype(np.float32)
        dst_tri = deformed[indices]
        if abs(cv2.contourArea(src_tri)) < 0.01 or abs(cv2.contourArea(dst_tri)) < 0.01:
            continue
        sx0 = max(0, int(math.floor(float(src_tri[:, 0].min()))))
        sy0 = max(0, int(math.floor(float(src_tri[:, 1].min()))))
        sx1 = min(width, int(math.ceil(float(src_tri[:, 0].max()))) + 1)
        sy1 = min(height, int(math.ceil(float(src_tri[:, 1].max()))) + 1)
        tx0 = max(0, int(math.floor(float(dst_tri[:, 0].min()))))
        ty0 = max(0, int(math.floor(float(dst_tri[:, 1].min()))))
        tx1 = min(width, int(math.ceil(float(dst_tri[:, 0].max()))) + 1)
        ty1 = min(height, int(math.ceil(float(dst_tri[:, 1].max()))) + 1)
        if sx1 <= sx0 or sy1 <= sy0 or tx1 <= tx0 or ty1 <= ty0:
            continue
        source_triangle = src_tri - np.array([sx0, sy0], dtype=np.float32)
        target_triangle = dst_tri - np.array([tx0, ty0], dtype=np.float32)
        affine = cv2.getAffineTransform(source_triangle, target_triangle)
        patch = cv2.warpAffine(
            source[sy0:sy1, sx0:sx1], affine, (tx1 - tx0, ty1 - ty0),
            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
        )
        triangle_mask = np.zeros((ty1 - ty0, tx1 - tx0), dtype=np.uint8)
        cv2.fillConvexPoly(triangle_mask, np.round(target_triangle).astype(np.int32), 255, lineType=cv2.LINE_AA)
        region = output[ty0:ty1, tx0:tx1]
        coverage = triangle_mask.astype(np.float32)[..., None] / 255.0
        source_alpha = patch[..., 3:4].astype(np.float32) / 255.0 * coverage
        destination_alpha = region[..., 3:4].astype(np.float32) / 255.0
        result_alpha = source_alpha + destination_alpha * (1.0 - source_alpha)
        source_rgb = patch[..., :3].astype(np.float32)
        destination_rgb = region[..., :3].astype(np.float32)
        premultiplied = source_rgb * source_alpha + destination_rgb * destination_alpha * (1.0 - source_alpha)
        nonzero = result_alpha[..., 0] > 1e-6
        blended = np.zeros_like(source_rgb)
        blended[nonzero] = premultiplied[nonzero] / result_alpha[nonzero]
        region[..., :3] = np.clip(blended, 0, 255).astype(np.uint8)
        region[..., 3:4] = np.clip(result_alpha * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(output)


def sample_clip(clip: dict[str, Any], frame: float) -> dict[str, float]:
    """Sample rotations with linear, smoothstep, or monotone cubic curves."""
    rotations: dict[str, float] = {}
    for joint_id, track in clip.get("tracks", {}).items():
        keys = sorted(track.get("rotation", []), key=lambda key: float(key["frame"]))
        if not keys:
            continue
        if frame <= float(keys[0]["frame"]):
            rotations[joint_id] = float(keys[0]["value"])
            continue
        if frame >= float(keys[-1]["frame"]):
            rotations[joint_id] = float(keys[-1]["value"])
            continue
        for key_index, (left, right) in enumerate(zip(keys, keys[1:])):
            start, end = float(left["frame"]), float(right["frame"])
            if start <= frame <= end:
                amount = (frame - start) / max(1e-6, end - start)
                interpolation = left.get("interpolation", "smooth")
                if interpolation == "smooth":
                    amount = amount * amount * (3.0 - 2.0 * amount)
                start_value, end_value = float(left["value"]), float(right["value"])
                if interpolation == "cubic":
                    duration = max(1e-6, end - start)
                    secant = (end_value - start_value) / duration

                    def monotone_tangent(before: float, after: float, h_before: float, h_after: float) -> float:
                        if before * after <= 0.0:
                            return 0.0
                        weight_before = 2.0 * h_after + h_before
                        weight_after = h_after + 2.0 * h_before
                        return (weight_before + weight_after) / (
                            weight_before / before + weight_after / after
                        )

                    previous = keys[key_index - 1] if key_index else None
                    right_index = key_index + 1
                    following = keys[right_index + 1] if right_index + 1 < len(keys) else None
                    if previous is None:
                        slope_start = secant
                    else:
                        p_frame, p_value = float(previous["frame"]), float(previous["value"])
                        previous_span = max(1e-6, start - p_frame)
                        previous_slope = (start_value - p_value) / previous_span
                        slope_start = monotone_tangent(previous_slope, secant, previous_span, duration)
                    if following is None:
                        slope_end = secant
                    else:
                        n_frame, n_value = float(following["frame"]), float(following["value"])
                        following_span = max(1e-6, n_frame - end)
                        following_slope = (n_value - end_value) / following_span
                        slope_end = monotone_tangent(secant, following_slope, duration, following_span)
                    t2, t3 = amount * amount, amount * amount * amount
                    h00, h10 = 2 * t3 - 3 * t2 + 1, t3 - 2 * t2 + amount
                    h01, h11 = -2 * t3 + 3 * t2, t3 - t2
                    rotations[joint_id] = (
                        h00 * start_value + h10 * duration * slope_start
                        + h01 * end_value + h11 * duration * slope_end
                    )
                else:
                    rotations[joint_id] = start_value + (end_value - start_value) * amount
                break
    return rotations


def sample_layer_visibility(clip: dict[str, Any], frame: float) -> dict[str, bool]:
    """Sample stepped layer visibility keys without blending between drawings."""
    visibility: dict[str, bool] = {}
    for layer_id, track in clip.get("layer_tracks", {}).items():
        keys = sorted(track.get("visibility", []), key=lambda key: float(key["frame"]))
        if not keys:
            continue
        selected = keys[0]
        for key in keys:
            if float(key["frame"]) > frame:
                break
            selected = key
        visibility[layer_id] = bool(selected["visible"])
    return visibility


def sample_expression(data: dict[str, Any], expression_id: str) -> tuple[dict[str, float], dict[str, float]]:
    expression = next((item for item in data.get("expressions", []) if item.get("id") == expression_id), None)
    if expression is None:
        raise ProjectError(f"No existe la expresión {expression_id}.")
    return (
        {key: float(value) for key, value in expression.get("joint_rotations", {}).items()},
        {key: float(value) for key, value in expression.get("layer_opacities", {}).items()},
    )


def solve_two_bone_ik(
    joints: list[dict[str, Any]],
    root_id: str,
    middle_id: str,
    tip_id: str,
    target_x: float,
    target_y: float,
    bend_direction: int = 1,
) -> dict[str, float]:
    """Return local rotations for a planar two-bone chain using the law of cosines."""
    by_id = {str(joint["id"]): joint for joint in joints}
    if any(joint_id not in by_id for joint_id in (root_id, middle_id, tip_id)):
        raise ProjectError("La cadena IK contiene una articulación inexistente.")
    if by_id[middle_id].get("parent") != root_id or by_id[tip_id].get("parent") != middle_id:
        raise ProjectError("Selecciona tres articulaciones en orden padre → codo/rodilla → extremo.")
    if bend_direction not in {-1, 1}:
        raise ProjectError("La dirección de flexión debe ser 1 o -1.")
    root, middle, tip = by_id[root_id], by_id[middle_id], by_id[tip_id]
    first_length = math.hypot(float(middle["x"]) - float(root["x"]), float(middle["y"]) - float(root["y"]))
    second_length = math.hypot(float(tip["x"]) - float(middle["x"]), float(tip["y"]) - float(middle["y"]))
    if first_length <= 1e-6 or second_length <= 1e-6:
        raise ProjectError("Los dos huesos deben tener longitud mayor que cero.")
    dx, dy = target_x - float(root["x"]), target_y - float(root["y"])
    raw_distance = math.hypot(dx, dy)
    low = abs(first_length - second_length) + 1e-4
    high = first_length + second_length - 1e-4
    distance = min(high, max(low, raw_distance))
    direction = math.atan2(dy, dx)
    root_cos = max(-1.0, min(1.0, (first_length**2 + distance**2 - second_length**2) / (2.0 * first_length * distance)))
    tip_cos = max(-1.0, min(1.0, (distance**2 + second_length**2 - first_length**2) / (2.0 * distance * second_length)))
    root_angle = direction - bend_direction * math.acos(root_cos)
    end_direction = direction + bend_direction * math.acos(tip_cos)
    bind_first = math.atan2(float(middle["y"]) - float(root["y"]), float(middle["x"]) - float(root["x"]))
    bind_second = math.atan2(float(tip["y"]) - float(middle["y"]), float(tip["x"]) - float(middle["x"]))
    root_local = root_angle - bind_first
    # The child rotation is local to its parent, so subtract the parent's local
    # rotation (not its world-space angle). Omitting bind_first made IK miss the
    # target by the setup angle of the upper bone and could flip the limb.
    middle_local = end_direction - bind_second - root_local
    return {root_id: math.degrees(root_local), middle_id: math.degrees(middle_local)}


def set_vertex_weight(
    path: str | Path,
    layer_id: str,
    vertex_index: int,
    joint_id: str,
    value: float,
    actor: str = "agent",
) -> None:
    if not 0.0 <= value <= 1.0:
        raise ProjectError("El peso debe estar entre 0 y 1.")
    project_path = Path(path).expanduser().resolve()
    data = load_project(project_path)
    layer = next((item for item in data.get("layers", []) if item.get("id") == layer_id), None)
    if layer is None or not layer.get("mesh"):
        raise ProjectError("La capa no tiene una malla generada.")
    if not any(joint.get("id") == joint_id for joint in data.get("joints", [])):
        raise ProjectError(f"No existe la articulación {joint_id}.")
    weights = layer["mesh"].get("weights", [])
    if not 0 <= vertex_index < len(weights):
        raise ProjectError("El índice de vértice queda fuera de la malla.")
    current = dict(weights[vertex_index])
    other = {key: weight for key, weight in current.items() if key != joint_id}
    if not other and value < 1.0:
        raise ProjectError("Un vértice necesita al menos una articulación con peso 1.")
    if value == 1.0:
        weights[vertex_index] = {joint_id: 1.0}
    else:
        other_total = sum(other.values())
        remainder = 1.0 - value
        normalized = {key: weight * remainder / other_total for key, weight in other.items()} if other_total else {}
        if value > 0:
            normalized[joint_id] = value
        weights[vertex_index] = normalized
    save_project(project_path, data, action=f"vertex_weight_set:{layer_id}:{vertex_index}:{joint_id}", actor=actor)


def render_project_pose(
    path: str | Path,
    rotations: dict[str, float] | None = None,
    layer_opacities: dict[str, float] | None = None,
    expression_id: str | None = None,
    layer_visibility: dict[str, bool] | None = None,
    facial_parameters: dict[str, float] | None = None,
    translation: tuple[float, float] | None = None,
) -> Image.Image:
    """Render a project with its bind-pose base and current mesh-deformed sprites."""
    project_path = Path(path).expanduser().resolve()
    data = load_project(project_path)
    rotations = rotations or {}
    layer_opacities = layer_opacities or {}
    layer_visibility = layer_visibility or {}
    facial_parameters = facial_parameters or {}
    if expression_id:
        expression_rotations, expression_opacities = sample_expression(data, expression_id)
        rotations = {**expression_rotations, **rotations}
        layer_opacities = {**expression_opacities, **layer_opacities}
    try:
        composite, cached_sprites = _render_resources(project_path, data)
        character = Image.new("RGBA", composite.size)
        ordered_layers = sorted(enumerate(data.get("layers", [])), key=lambda pair: (pair[1].get("draw_order", pair[0]), pair[0]))
        for _, layer in ordered_layers:
            if not layer_visibility.get(layer["id"], layer.get("visible", True)) or not layer.get("sprite"):
                continue
            sprite = cached_sprites[layer["id"]].copy()
            if layer.get("mesh"):
                sprite = deform_layer_sprite(sprite, layer["mesh"], data.get("joints", []), rotations)
            opacity = float(layer_opacities.get(layer["id"], layer.get("opacity", 1.0)))
            label = str(layer.get("name", "")).casefold()
            if any(token in label for token in ("eyewhite", "eye white", "iris", "irides", "pupil", "pupila")):
                opacity *= max(0.0, min(1.0, float(facial_parameters.get("eye_open", 1.0))))
            if any(token in label for token in ("iris", "irides", "pupil", "pupila")):
                dx = round(float(facial_parameters.get("gaze_x", 0.0)) * 7)
                dy = round(float(facial_parameters.get("gaze_y", 0.0)) * 4)
                if dx or dy:
                    shifted = Image.new("RGBA", sprite.size)
                    shifted.alpha_composite(sprite, (dx, dy))
                    sprite = shifted
            if any(token in label for token in ("eyebrow", "ceja")):
                dy = round(-float(facial_parameters.get("brow_raise", 0.0)) * 3)
                if dy:
                    shifted = Image.new("RGBA", sprite.size)
                    shifted.alpha_composite(sprite, (0, dy))
                    sprite = shifted
            if "mouth" in label or "boca" in label:
                opening = max(0.0, min(1.0, float(facial_parameters.get("mouth_open", 0.0))))
                if opening > 0.04:
                    bounds = sprite.getchannel("A").getbbox()
                    if bounds:
                        x0, y0, x1, y1 = bounds
                        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                        width = max(3, (x1 - x0) * (0.58 + 0.55 * float(facial_parameters.get("mouth_wide", 0.0))))
                        height = max(2, (y1 - y0) * (0.12 + 0.9 * opening))
                        draw = ImageDraw.Draw(sprite)
                        draw.rectangle(bounds, fill=(0, 0, 0, 0))
                        draw.ellipse((cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2), fill=(63, 25, 38, 255))
                        inner_h = height * (0.35 + 0.3 * float(facial_parameters.get("mouth_round", 0.0)))
                        draw.ellipse((cx - width * 0.3, cy + height * 0.08, cx + width * 0.3, cy + inner_h), fill=(199, 87, 111, 230))
            sprite.putalpha(sprite.getchannel("A").point(lambda value: round(value * max(0.0, min(1.0, opacity)))))
            character.alpha_composite(sprite)
        composite.alpha_composite(character)
        if translation and (abs(translation[0]) > 1e-6 or abs(translation[1]) > 1e-6):
            corners = [composite.getpixel(point) for point in ((0, 0), (composite.width - 1, 0), (0, composite.height - 1), (composite.width - 1, composite.height - 1))]
            fill = tuple(round(sum(pixel[channel] for pixel in corners) / len(corners)) for channel in range(4))
            composite = composite.transform(
                composite.size, Image.Transform.AFFINE,
                (1, 0, -round(translation[0]), 0, 1, -round(translation[1])),
                resample=Image.Resampling.NEAREST, fillcolor=fill,
            )
        return composite
    except OSError as exc:
        raise ProjectError(f"No pude renderizar la pose: {exc}") from exc
