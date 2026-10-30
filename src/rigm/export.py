"""Portable RigM runtime bundle exporter."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from .project import ProjectError, load_project


def export_bundle(project: str | Path, output: str | Path) -> Path:
    project_path = Path(project).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    data = load_project(project_path)
    payload: dict[str, Any] = {key: value for key, value in data.items() if not key.startswith("_")}
    references: set[str] = set()
    for field in ("image", "base_image"):
        value = payload.get(field)
        if value:
            references.add(str(value))
    for layer in payload.get("layers", []):
        for field in ("mask", "sprite"):
            if layer.get(field):
                references.add(str(layer[field]))
        for proposal in layer.get("mask_proposals", []):
            if proposal.get("mask"):
                references.add(str(proposal["mask"]))
    avatar = payload.get("avatar3d")
    if isinstance(avatar, dict):
        for field in ("path", "source_path"):
            if avatar.get(field):
                references.add(str(avatar[field]))
    assets: list[tuple[str, Path]] = []
    root = project_path.parent.resolve()
    for reference in sorted(references):
        ref = Path(reference)
        if ref.is_absolute():
            try:
                ref = ref.resolve().relative_to(root)
            except ValueError as exc:
                raise ProjectError(f"El recurso {reference} está fuera de la carpeta del proyecto; hazlo portable antes de exportar.") from exc
        source = (root / ref).resolve()
        try:
            archive_name = source.relative_to(root).as_posix()
        except ValueError as exc:
            raise ProjectError(f"La ruta de recurso escapa de la carpeta del proyecto: {reference}") from exc
        if not source.is_file():
            raise ProjectError(f"No puedo exportar; falta el recurso {reference}.")
        assets.append((archive_name, source))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("project.rigm", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            for archive_name, source in assets:
                archive.write(source, archive_name)
            archive.writestr(
                "README.txt",
                "RigM portable runtime bundle. Open project.rigm with a compatible RigM runtime.\n",
            )
    except OSError as exc:
        raise ProjectError(f"No pude crear el paquete de exportación: {exc}") from exc
    return output_path
