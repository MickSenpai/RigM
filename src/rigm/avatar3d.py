"""Portable GLB/VRM avatar import, validation and optional Blender auto-rig."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .project import ProjectError, load_project, save_project


VRM_REQUIRED_HUMAN_BONES = (
    "hips", "spine", "head",
    "leftUpperArm", "leftLowerArm", "leftHand",
    "rightUpperArm", "rightLowerArm", "rightHand",
    "leftUpperLeg", "leftLowerLeg", "leftFoot",
    "rightUpperLeg", "rightLowerLeg", "rightFoot",
)
VRM_REQUIRED_EXPRESSIONS = ("blink", "aa", "ih", "ou", "ee", "oh")


def read_glb_document(path: str | Path) -> dict[str, Any]:
    """Read the JSON chunk of a GLB/VRM without loading mesh buffers."""
    source = Path(path).expanduser().resolve()
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ProjectError(f"No pude leer el avatar 3D {source}: {exc}") from exc
    if len(raw) < 20 or raw[:4] != b"glTF":
        raise ProjectError("El avatar no es un contenedor GLB/VRM válido (falta la cabecera glTF).")
    version = int.from_bytes(raw[4:8], "little")
    declared_length = int.from_bytes(raw[8:12], "little")
    if version != 2 or declared_length != len(raw):
        raise ProjectError(f"GLB no compatible: versión {version}, tamaño declarado {declared_length}, tamaño real {len(raw)}.")
    json_length = int.from_bytes(raw[12:16], "little")
    json_type = raw[16:20]
    if json_type != b"JSON" or 20 + json_length > len(raw):
        raise ProjectError("GLB inválido: el primer chunk no es JSON o está truncado.")
    try:
        document = json.loads(raw[20:20 + json_length].decode("utf-8").rstrip(" \t\r\n\0"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectError(f"El chunk JSON del GLB no se puede leer: {exc}") from exc
    asset = document.get("asset") if isinstance(document, dict) else None
    if not isinstance(asset, dict) or asset.get("version") != "2.0":
        raise ProjectError("El documento no declara glTF 2.0.")
    return document


def inspect_vrm(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    document = read_glb_document(source)
    extensions = document.get("extensions", {})
    if not isinstance(extensions, dict):
        raise ProjectError("El objeto extensions del GLB debe ser un mapa JSON.")
    vrm = extensions.get("VRMC_vrm")
    if not isinstance(vrm, dict):
        legacy = "VRM" in extensions or "VRM" in document.get("extensionsUsed", [])
        if legacy:
            raise ProjectError("Este modelo usa VRM 0.x. RigM requiere VRM 1.0 para mantener una asignación humanoide y expresiones interoperables.")
        raise ProjectError("El GLB no contiene la extensión VRMC_vrm; importa un avatar VRM 1.0.")
    meta = vrm.get("meta", {})
    if not isinstance(meta, dict):
        raise ProjectError("VRMC_vrm.meta no tiene el formato esperado.")
    spec_version = str(vrm.get("specVersion", ""))
    if spec_version != "1.0":
        raise ProjectError(f"Versión VRM no compatible: {spec_version or 'sin declarar'}; se requiere 1.0.")
    humanoid_data = vrm.get("humanoid", {})
    if not isinstance(humanoid_data, dict) or not isinstance(humanoid_data.get("humanBones", {}), dict):
        raise ProjectError("VRMC_vrm.humanoid.humanBones no tiene el formato esperado.")
    humanoid = humanoid_data.get("humanBones", {})
    nodes = document.get("nodes", [])
    if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
        raise ProjectError("El arreglo nodes del GLB no tiene el formato esperado.")
    assigned = {
        name: int(entry["node"])
        for name, entry in humanoid.items()
        if isinstance(entry, dict) and isinstance(entry.get("node"), int)
    }
    invalid_nodes = [name for name, index in assigned.items() if index < 0 or index >= len(nodes)]
    missing_bones = [bone for bone in VRM_REQUIRED_HUMAN_BONES if bone not in assigned]
    expressions = vrm.get("expressions", {})
    preset_expressions = expressions.get("preset", {}) if isinstance(expressions, dict) else {}
    if not isinstance(preset_expressions, dict):
        raise ProjectError("VRMC_vrm.expressions.preset no tiene el formato esperado.")
    animation_data = document.get("animations", [])
    if not isinstance(animation_data, list):
        raise ProjectError("El arreglo animations del GLB no tiene el formato esperado.")
    animations = [
        str(clip.get("name") or f"Animation {index + 1}")
        for index, clip in enumerate(animation_data)
        if isinstance(clip, dict)
    ]
    scene_index = document.get("scene", 0)
    if not isinstance(scene_index, int):
        raise ProjectError("La escena predeterminada del GLB debe ser un índice entero.")
    scenes = document.get("scenes", [])
    if not isinstance(scenes, list):
        raise ProjectError("El arreglo scenes del GLB no tiene el formato esperado.")
    if any(not isinstance(scene, dict) for scene in scenes):
        raise ProjectError("Cada escena del GLB debe ser un objeto JSON.")
    root_nodes = scenes[scene_index].get("nodes", []) if 0 <= scene_index < len(scenes) else []
    skins = document.get("skins", [])
    if not isinstance(skins, list):
        raise ProjectError("El arreglo skins del GLB no tiene el formato esperado.")
    return {
        "path": str(source),
        "name": str(meta.get("name") or source.stem),
        "spec_version": spec_version,
        "node_count": len(nodes),
        "skin_count": len(skins),
        "root_node_count": len(root_nodes),
        "human_bones": sorted(assigned),
        "missing_required_bones": missing_bones,
        "invalid_bone_nodes": invalid_nodes,
        "expressions": sorted(preset_expressions),
        "missing_required_expressions": [name for name in VRM_REQUIRED_EXPRESSIONS if name not in preset_expressions],
        "animations": animations,
        "look_at": bool(vrm.get("lookAt")),
        "spring_bones": "VRMC_springBone" in extensions,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


def inspect_glb(path: str | Path) -> dict[str, Any]:
    """Inspect a plain GLB, with or without an existing humanoid skeleton."""
    source = Path(path).expanduser().resolve()
    document = read_glb_document(source)
    nodes = document.get("nodes", [])
    meshes = document.get("meshes", [])
    skins = document.get("skins", [])
    animations = document.get("animations", [])
    if (
        not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes)
        or not isinstance(meshes, list) or not all(isinstance(mesh, dict) for mesh in meshes)
        or not isinstance(skins, list) or not isinstance(animations, list)
    ):
        raise ProjectError("El GLB contiene arreglos glTF con formato inválido.")
    primitive_count = sum(len(mesh.get("primitives", [])) for mesh in meshes if isinstance(mesh.get("primitives", []), list))
    skin_nodes = set()
    invalid_skin_joints = []
    empty_skins = 0
    for skin in skins:
        if not isinstance(skin, dict) or not isinstance(skin.get("joints", []), list):
            raise ProjectError("Una skin del GLB no declara una lista válida de joints.")
        joints = skin["joints"]
        if not joints:
            empty_skins += 1
        for index in joints:
            if not isinstance(index, int) or index < 0 or index >= len(nodes):
                invalid_skin_joints.append(index)
            else:
                skin_nodes.add(index)
    bone_names = sorted(
        str(nodes[index].get("name", f"node-{index}"))
        for index in skin_nodes
        if 0 <= index < len(nodes) and isinstance(nodes[index], dict)
    )
    return {
        "path": str(source), "name": source.stem,
        "node_count": len(nodes), "mesh_count": len(meshes), "primitive_count": primitive_count,
        "skin_count": len(skins), "rigged": bool(skin_nodes),
        "invalid_skin_joints": invalid_skin_joints, "empty_skins": empty_skins,
        "human_bones": bone_names,
        "animations": [str(item.get("name") or f"Animation {i + 1}") for i, item in enumerate(animations) if isinstance(item, dict)],
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


def validate_glb(path: str | Path) -> dict[str, Any]:
    info = inspect_glb(path)
    errors: list[str] = []
    warnings: list[str] = []
    if not info["mesh_count"] or not info["primitive_count"]:
        errors.append("El GLB no contiene mallas que RigM pueda mostrar o riggear.")
    if info["invalid_skin_joints"]:
        errors.append("Una skin referencia nodos inexistentes o índices de hueso inválidos.")
    if info["empty_skins"]:
        errors.append("El GLB contiene skins sin huesos asignados.")
    if not info["rigged"]:
        warnings.append("Este GLB aún no tiene esqueleto. Puedes previsualizarlo o pedir a RigM que proponga un auto-rig con Blender.")
    if info["rigged"] and not info["animations"]:
        warnings.append("El GLB tiene skin, pero no trae clips de animación.")
    return {"valid": not errors, "errors": errors, "warnings": warnings, **info}


def attach_avatar(project_path: str | Path, model_path: str | Path, *, actor: str = "user", extra_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Copy a VRM or GLB avatar into a project without altering the source."""
    source = Path(model_path).expanduser().resolve()
    suffix = source.suffix.casefold()
    if suffix == ".vrm":
        info = validate_vrm(source)
        resource_format, version = "vrm", "1.0"
    elif suffix == ".glb":
        info = validate_glb(source)
        resource_format, version = "glb", "2.0"
    else:
        raise ProjectError("Formato no compatible. Importa un archivo .glb o .vrm.")
    if info["errors"]:
        raise ProjectError("El avatar no pasó la validación: " + " ".join(info["errors"]))
    project_file = Path(project_path).expanduser().resolve()
    data = load_project(project_file)
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", source.stem).strip("-").lower() or "avatar"
    relative = Path("assets") / "avatars" / f"{slug}-{info['sha256'][:10]}{suffix}"
    destination = project_file.parent / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    created_resource = not destination.exists()
    if created_resource:
        temporary = destination.with_name(f".{destination.name}.tmp")
        try:
            shutil.copy2(source, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    data["avatar3d"] = {
        "format": resource_format, "version": version, "path": relative.as_posix(),
        "sha256": info["sha256"], "name": info["name"],
        "rigged": info.get("rigged", True), "human_bones": info["human_bones"],
        "animations": info["animations"],
        **(extra_metadata or {}),
    }
    data["status"] = "needs_review"
    data["stage"] = "avatar3d"
    warning_summary = " ".join(info["warnings"])
    data["last_result"] = "Avatar 3D importado; requiere revisión." + (" " + warning_summary if warning_summary else "")
    try:
        save_project(project_file, data, action="avatar3d_attached", actor=actor)
    except Exception:
        if created_resource and destination.exists():
            destination.unlink()
        raise
    return {"project": str(project_file), "avatar": data["avatar3d"], "validation": info}


def validate_vrm(path: str | Path) -> dict[str, Any]:
    info = inspect_vrm(path)
    errors = []
    warnings = []
    if info["invalid_bone_nodes"]:
        errors.append("Huesos humanoides apuntan a nodos inexistentes: " + ", ".join(info["invalid_bone_nodes"]) + ".")
    if info["missing_required_bones"]:
        errors.append("Faltan huesos humanoides requeridos: " + ", ".join(info["missing_required_bones"]) + ".")
    if info["root_node_count"] == 0:
        errors.append("La escena glTF predeterminada no tiene nodo raíz.")
    if not info["skin_count"]:
        errors.append("El avatar no tiene skin esquelético.")
    if info["missing_required_expressions"]:
        warnings.append("Faltan expresiones faciales estándar: " + ", ".join(info["missing_required_expressions"]) + ".")
    if not info["look_at"]:
        warnings.append("El avatar no configura control VRM LookAt; la mirada requerirá huesos o expresiones compatibles.")
    if not info["spring_bones"]:
        warnings.append("El avatar no incluye VRMC_springBone; pelo y ropa no tendrán movimiento secundario VRM.")
    if not info["animations"]:
        warnings.append("El avatar no trae clips de movimiento. El visor usará movimiento procedural acotado; caminar con naturalidad requiere un clip de locomoción revisado.")
    return {"valid": not errors, "errors": errors, "warnings": warnings, **info}


def attach_vrm(project_path: str | Path, model_path: str | Path, *, actor: str = "user") -> dict[str, Any]:
    """Backward-compatible VRM-specific import entrypoint."""
    return attach_avatar(project_path, model_path, actor=actor)


def autorig_glb(project_path: str | Path, *, blender_path: str | None = None, forward_axis: str = "-Y", actor: str = "user", expected_mtime_ns: int | None = None) -> dict[str, Any]:
    """Generate an editable humanoid skeleton/weights for the project's unrigged GLB using Blender."""
    if forward_axis not in {"-Y", "+Y", "-X", "+X"}:
        raise ProjectError("El eje frontal debe ser -Y, +Y, -X o +X.")
    executable = blender_path or shutil.which("blender")
    if not executable:
        raise ProjectError("No encuentro Blender. Instala Blender y vuelve a intentarlo, o indica su ejecutable con --blender.")
    project_file = Path(project_path).expanduser().resolve()
    data = load_project(project_file)
    avatar = data.get("avatar3d", {})
    if not isinstance(avatar, dict) or avatar.get("format") != "glb":
        raise ProjectError("Primero importa un archivo GLB al proyecto.")
    if avatar.get("rigged"):
        raise ProjectError("El GLB ya declara una skin. Abre el visor o importa una copia sin rig para crear otro esqueleto.")
    source_relative = Path(str(avatar.get("path", "")))
    source = (project_file.parent / source_relative).resolve()
    try:
        source.relative_to(project_file.parent.resolve())
    except ValueError as exc:
        raise ProjectError("La ruta del GLB sale de la carpeta del proyecto.") from exc
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != avatar.get("sha256"):
        raise ProjectError("El GLB original falta o cambió desde que se importó; vuelve a importarlo antes de riggear.")
    script = Path(__file__).with_name("blender_autorig.py")
    with tempfile.TemporaryDirectory(prefix="rigm-autorig-") as temporary_dir:
        output = Path(temporary_dir) / "rigged.glb"
        report_file = Path(temporary_dir) / "analysis.json"
        command = [str(executable), "--background", "--factory-startup", "--python-exit-code", "1", "--python", str(script), "--", str(source), str(output), str(report_file), forward_axis]
        try:
            process = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectError(f"No pude ejecutar el auto-rig de Blender: {exc}") from exc
        if process.returncode != 0 or not output.is_file() or not report_file.is_file():
            detail = process.stderr or process.stdout or "Blender terminó sin generar el GLB riggeado."
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            detail = detail[-3500:]
            raise ProjectError("Falló el auto-rig en Blender. El archivo fuente sigue intacto. Detalle: " + detail)
        try:
            report = json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectError(f"Blender generó el modelo, pero no pude leer su informe: {exc}") from exc
        result = validate_glb(output)
        if result["errors"] or not result["rigged"]:
            raise ProjectError("El GLB resultante no contiene una skin utilizable: " + " ".join(result["errors"]))
        if expected_mtime_ns is not None and project_file.stat().st_mtime_ns != expected_mtime_ns:
            raise ProjectError("El proyecto cambió mientras Blender trabajaba. No guardé el auto-rig; recárgalo e inténtalo de nuevo.")
        source_hash = avatar["sha256"]
        source_path = source_relative.as_posix()
        attached = attach_avatar(project_file, output, actor=actor, extra_metadata={
            "name": avatar.get("name", source.stem),
            "source_path": source_path, "source_sha256": source_hash,
            "rigging": {"method": "blender_geometry_autorig", "forward_axis": forward_axis, **report},
        })
    return {**attached, "analysis": report}


def validate_project_avatar(data: dict[str, Any], project_path: str | Path) -> dict[str, Any] | None:
    if "avatar3d" not in data:
        return None
    avatar = data.get("avatar3d")
    if not isinstance(avatar, dict) or (avatar.get("format"), avatar.get("version")) not in {("vrm", "1.0"), ("glb", "2.0")}:
        return {"errors": ["El recurso avatar3d debe ser VRM 1.0 o GLB 2.0."], "warnings": []}
    relative = Path(str(avatar.get("path", "")))
    root = Path(project_path).expanduser().resolve().parent
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        return {"errors": ["La ruta de avatar3d debe ser relativa y permanecer dentro del proyecto."], "warnings": []}
    model = (root / relative).resolve()
    try:
        model.relative_to(root)
    except ValueError:
        return {"errors": ["La ruta del avatar sale de la carpeta del proyecto."], "warnings": []}
    if not model.is_file():
        return {"errors": [f"No encuentro el avatar 3D: {relative.as_posix()}."], "warnings": []}
    actual_hash = hashlib.sha256(model.read_bytes()).hexdigest()
    if avatar.get("sha256") != actual_hash:
        return {"errors": ["El hash del avatar VRM no coincide con el recurso registrado."], "warnings": []}
    result = validate_vrm(model) if avatar["format"] == "vrm" else validate_glb(model)
    source_relative = avatar.get("source_path")
    if source_relative:
        source = (root / str(source_relative)).resolve()
        try:
            source.relative_to(root)
        except ValueError:
            return {"errors": ["La ruta GLB de origen sale de la carpeta del proyecto."], "warnings": []}
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != avatar.get("source_sha256"):
            return {"errors": ["El GLB fuente del auto-rig falta o cambió."], "warnings": []}
    return {"errors": result["errors"], "warnings": result["warnings"], "avatar": result}
