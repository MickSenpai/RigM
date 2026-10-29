"""Structured command interface for people and AI agents operating RigM."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .project import (
    ProjectError,
    ProjectLibrary,
    accept_layer_mask_proposal,
    add_joint,
    connect_joints,
    create_layer,
    create_project,
    export_composite,
    extract_layer,
    inpaint_layer_holes,
    import_layer_mask,
    load_project,
    move_joint,
    propose_layer_masks,
    update_workflow,
    validate_project,
    create_animation_clip,
    create_expression,
    set_expression_value,
    set_joint_parent,
    set_rotation_key,
)
from .export import export_bundle
from .rig import generate_layer_mesh, render_project_pose, sample_clip, set_vertex_weight, solve_two_bone_ik
from .decomposition import decompose_project_with_see_through


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rigmctl", description="Operaciones estructuradas para RigM y agentes de IA.")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="Lista proyectos registrados y valida si existen.")
    listing.add_argument("--json", action="store_true")

    create = sub.add_parser("create", help="Crea un proyecto portable desde una imagen.")
    create.add_argument("image")
    create.add_argument("output")
    create.add_argument("--name")

    inspect = sub.add_parser("inspect", help="Muestra el manifiesto del proyecto.")
    inspect.add_argument("project")

    validate = sub.add_parser("validate", help="Comprueba el archivo, la imagen y la estructura del esqueleto.")
    validate.add_argument("project")

    joint = sub.add_parser("joint", help="Gestiona pivotes.")
    joint_sub = joint.add_subparsers(dest="joint_action", required=True)
    add = joint_sub.add_parser("add")
    add.add_argument("project")
    add.add_argument("name")
    add.add_argument("x", type=float)
    add.add_argument("y", type=float)
    move = joint_sub.add_parser("move")
    move.add_argument("project")
    move.add_argument("joint_id")
    move.add_argument("x", type=float)
    move.add_argument("y", type=float)
    parent = joint_sub.add_parser("parent", help="Asigna el padre de una articulación; usa 'root' para separar la cadena.")
    parent.add_argument("project")
    parent.add_argument("joint_id")
    parent.add_argument("parent_id")

    layer = sub.add_parser("layer", help="Crea capas de máscara o importa máscaras generadas por IA.")
    layer_sub = layer.add_subparsers(dest="layer_action", required=True)
    layer_add = layer_sub.add_parser("add", help="Crea una máscara vacía del tamaño de la fuente.")
    layer_add.add_argument("project")
    layer_add.add_argument("name")
    layer_import = layer_sub.add_parser("import-mask", help="Añade una máscara PNG existente como capa.")
    layer_import.add_argument("project")
    layer_import.add_argument("name")
    layer_import.add_argument("mask")
    layer_extract = layer_sub.add_parser("extract", help="Extrae la pieza RGBA de una capa usando su máscara.")
    layer_extract.add_argument("project")
    layer_extract.add_argument("layer_id")
    layer_inpaint = layer_sub.add_parser("inpaint", help="Rellena la fuente bajo las máscaras usando OpenCV.")
    layer_inpaint.add_argument("project")
    layer_inpaint.add_argument("--radius", type=float, default=3.0)
    layer_inpaint.add_argument("--method", choices=("telea", "ns"), default="telea")
    layer_render = layer_sub.add_parser("render", help="Exporta una composición de base reconstruida y piezas.")
    layer_render.add_argument("project")
    layer_render.add_argument("output")
    layer_propose = layer_sub.add_parser("propose", help="Solicita propuestas SAM 2 con clics y/o una caja.")
    layer_propose.add_argument("project")
    layer_propose.add_argument("layer_id")
    layer_propose.add_argument("--positive", nargs=2, type=float, action="append", default=[])
    layer_propose.add_argument("--negative", nargs=2, type=float, action="append", default=[])
    layer_propose.add_argument("--box", nargs=4, type=float)
    layer_propose.add_argument("--model", default="facebook/sam2.1-hiera-tiny")
    layer_accept = layer_sub.add_parser("accept", help="Acepta una propuesta y la usa como máscara editable.")
    layer_accept.add_argument("project")
    layer_accept.add_argument("layer_id")
    layer_accept.add_argument("proposal_id")

    mesh = sub.add_parser("mesh", help="Genera una malla deformable para una pieza extraída.")
    mesh_sub = mesh.add_subparsers(dest="mesh_action", required=True)
    mesh_generate = mesh_sub.add_parser("generate")
    mesh_generate.add_argument("project")
    mesh_generate.add_argument("layer_id")
    mesh_generate.add_argument("--spacing", type=int, default=28)

    weight = sub.add_parser("weight", help="Edita los pesos normalizados de un vértice de malla.")
    weight_sub = weight.add_subparsers(dest="weight_action", required=True)
    weight_set = weight_sub.add_parser("set")
    weight_set.add_argument("project")
    weight_set.add_argument("layer_id")
    weight_set.add_argument("vertex", type=int)
    weight_set.add_argument("joint_id")
    weight_set.add_argument("value", type=float)

    clip = sub.add_parser("clip", help="Crea clips y claves de rotación para articulaciones.")
    clip_sub = clip.add_subparsers(dest="clip_action", required=True)
    clip_create = clip_sub.add_parser("create")
    clip_create.add_argument("project")
    clip_create.add_argument("name")
    clip_create.add_argument("--duration", type=int, default=60)
    clip_create.add_argument("--fps", type=int, default=30)
    clip_create.add_argument("--no-loop", action="store_true")
    clip_key = clip_sub.add_parser("key")
    clip_key.add_argument("project")
    clip_key.add_argument("clip_id")
    clip_key.add_argument("joint_id")
    clip_key.add_argument("frame", type=int)
    clip_key.add_argument("rotation", type=float)
    clip_key.add_argument("--interpolation", choices=("smooth", "linear"), default="smooth")

    expression = sub.add_parser("expression", help="Crea expresiones de articulaciones y opacidad por capa.")
    expression_sub = expression.add_subparsers(dest="expression_action", required=True)
    expression_create = expression_sub.add_parser("create")
    expression_create.add_argument("project")
    expression_create.add_argument("name")
    expression_set = expression_sub.add_parser("set")
    expression_set.add_argument("project")
    expression_set.add_argument("expression_id")
    expression_set.add_argument("target_id")
    expression_set.add_argument("value", type=float)
    expression_set.add_argument("--type", choices=("layer_opacity", "joint_rotation"), default="layer_opacity")

    ik = sub.add_parser("ik", help="Resuelve una cadena plana de dos huesos hacia un objetivo.")
    ik_sub = ik.add_subparsers(dest="ik_action", required=True)
    ik_solve = ik_sub.add_parser("solve")
    ik_solve.add_argument("project")
    ik_solve.add_argument("root_id")
    ik_solve.add_argument("middle_id")
    ik_solve.add_argument("tip_id")
    ik_solve.add_argument("x", type=float)
    ik_solve.add_argument("y", type=float)

    pose = sub.add_parser("pose", help="Renderiza una pose, un fotograma o una expresión.")
    pose_sub = pose.add_subparsers(dest="pose_action", required=True)
    pose_render = pose_sub.add_parser("render")
    pose_render.add_argument("project")
    pose_render.add_argument("output")
    pose_render.add_argument("--clip")
    pose_render.add_argument("--frame", type=float, default=0)
    pose_render.add_argument("--expression")
    pose_render.add_argument("--rotation", nargs=2, action="append", default=[], metavar=("JOINT_ID", "DEGREES"))

    export = sub.add_parser("export", help="Empaqueta el proyecto y sus recursos como un bundle portable.")
    export.add_argument("project")
    export.add_argument("output")

    decompose = sub.add_parser("decompose", help="Separa una ilustración anime en capas con See-through en la nube.")
    decompose.add_argument("project")
    decompose.add_argument("--seed", type=int, default=42)
    decompose.add_argument("--allow-upload", action="store_true", help="Confirma que la imagen puede enviarse a Hugging Face.")

    bone = sub.add_parser("bone", help="Conecta dos pivotes existentes.")
    bone_sub = bone.add_subparsers(dest="bone_action", required=True)
    connect = bone_sub.add_parser("connect")
    connect.add_argument("project")
    connect.add_argument("start_id")
    connect.add_argument("end_id")

    status = sub.add_parser("status", help="Actualiza etapa y estado del trabajo autónomo.")
    status.add_argument("project")
    status.add_argument("state", choices=("queued", "in_progress", "needs_review", "ready", "failed"))
    status.add_argument("stage")
    status.add_argument("--detail", default="")
    status.add_argument("--progress", type=int)
    status.add_argument("--task-id")
    return parser


def _emit(value: object, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "list":
            _emit(ProjectLibrary().list_projects(), args.json)
        elif args.command == "create":
            path = create_project(args.image, args.output, args.name)
            ProjectLibrary().add(path)
            _emit({"created": str(path), "project": load_project(path)})
        elif args.command == "inspect":
            project = load_project(args.project)
            _emit({key: value for key, value in project.items() if not key.startswith("_")})
        elif args.command == "validate":
            result = validate_project(args.project)
            _emit(result)
            if result["errors"]:
                raise SystemExit(2)
        elif args.command == "joint" and args.joint_action == "add":
            joint_id = add_joint(args.project, args.name, args.x, args.y)
            _emit({"joint_id": joint_id})
        elif args.command == "joint" and args.joint_action == "move":
            move_joint(args.project, args.joint_id, args.x, args.y)
            _emit({"moved": args.joint_id})
        elif args.command == "joint" and args.joint_action == "parent":
            set_joint_parent(args.project, args.joint_id, None if args.parent_id == "root" else args.parent_id)
            _emit({"joint_id": args.joint_id, "parent_id": None if args.parent_id == "root" else args.parent_id})
        elif args.command == "layer" and args.layer_action == "add":
            layer_id = create_layer(args.project, args.name)
            _emit({"layer_id": layer_id})
        elif args.command == "layer" and args.layer_action == "import-mask":
            layer_id = import_layer_mask(args.project, args.name, args.mask)
            _emit({"layer_id": layer_id})
        elif args.command == "layer" and args.layer_action == "extract":
            sprite = extract_layer(args.project, args.layer_id)
            _emit({"sprite": str(sprite)})
        elif args.command == "layer" and args.layer_action == "inpaint":
            image = inpaint_layer_holes(args.project, args.radius, args.method)
            _emit({"base_image": str(image), "review_required": True})
        elif args.command == "layer" and args.layer_action == "render":
            output = export_composite(args.project, args.output)
            _emit({"composite": str(output)})
        elif args.command == "layer" and args.layer_action == "propose":
            proposals = propose_layer_masks(
                args.project,
                args.layer_id,
                [tuple(point) for point in args.positive],
                [tuple(point) for point in args.negative],
                tuple(args.box) if args.box else None,
                args.model,
            )
            _emit({"proposals": proposals})
        elif args.command == "layer" and args.layer_action == "accept":
            mask = accept_layer_mask_proposal(args.project, args.layer_id, args.proposal_id)
            _emit({"accepted_mask": str(mask)})
        elif args.command == "mesh" and args.mesh_action == "generate":
            mesh = generate_layer_mesh(args.project, args.layer_id, args.spacing)
            _emit({"layer_id": args.layer_id, "vertex_count": len(mesh["points"]), "triangle_count": len(mesh["triangles"])})
        elif args.command == "weight" and args.weight_action == "set":
            set_vertex_weight(args.project, args.layer_id, args.vertex, args.joint_id, args.value)
            _emit({"layer_id": args.layer_id, "vertex": args.vertex, "joint_id": args.joint_id, "weight": args.value})
        elif args.command == "clip" and args.clip_action == "create":
            clip_id = create_animation_clip(args.project, args.name, args.duration, args.fps, not args.no_loop)
            _emit({"clip_id": clip_id})
        elif args.command == "clip" and args.clip_action == "key":
            set_rotation_key(args.project, args.clip_id, args.joint_id, args.frame, args.rotation, args.interpolation)
            _emit({"clip_id": args.clip_id, "joint_id": args.joint_id, "frame": args.frame})
        elif args.command == "expression" and args.expression_action == "create":
            expression_id = create_expression(args.project, args.name)
            _emit({"expression_id": expression_id})
        elif args.command == "expression" and args.expression_action == "set":
            set_expression_value(args.project, args.expression_id, args.target_id, args.value, args.type)
            _emit({"expression_id": args.expression_id, "target_id": args.target_id, "value": args.value})
        elif args.command == "ik" and args.ik_action == "solve":
            result = solve_two_bone_ik(load_project(args.project)["joints"], args.root_id, args.middle_id, args.tip_id, args.x, args.y)
            _emit({"rotations": result, "target": [args.x, args.y]})
        elif args.command == "pose" and args.pose_action == "render":
            rotations = {joint_id: float(value) for joint_id, value in args.rotation}
            if args.clip:
                project_data = load_project(args.project)
                clip_data = next((item for item in project_data.get("animations", []) if item.get("id") == args.clip), None)
                if clip_data is None:
                    raise ProjectError(f"No existe el clip {args.clip}.")
                clip_frame = args.frame % clip_data["duration"] if clip_data.get("loop") and clip_data["duration"] else args.frame
                rotations = {**sample_clip(clip_data, clip_frame), **rotations}
            image = render_project_pose(args.project, rotations, expression_id=args.expression)
            output = Path(args.output).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            image.save(output)
            _emit({"render": str(output), "frame": args.frame, "clip": args.clip, "expression": args.expression})
        elif args.command == "export":
            output = export_bundle(args.project, args.output)
            _emit({"bundle": str(output)})
        elif args.command == "decompose":
            if not args.allow_upload:
                raise ProjectError("Esta operación envía una copia de la imagen a Hugging Face. Repite con --allow-upload si lo autorizas.")
            result = decompose_project_with_see_through(args.project, seed=args.seed)
            _emit(result)
        elif args.command == "bone" and args.bone_action == "connect":
            connect_joints(args.project, args.start_id, args.end_id)
            _emit({"connected": [args.start_id, args.end_id]})
        elif args.command == "status":
            update_workflow(args.project, args.state, args.stage, args.detail, args.progress, args.task_id)
            _emit({"status": args.state, "stage": args.stage, "progress": args.progress, "task_id": args.task_id, "detail": args.detail})
    except ProjectError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
