"""Blender background script used by RigM's supervised GLB auto-rig command.

Run by avatar3d.autorig_glb; this file intentionally has no RigM imports because
Blender ships its own Python runtime.
"""

import bpy
import json
import math
import sys
from mathutils import Matrix, Vector


def fail(message):
    print("RIGM_AUTORIG_ERROR: " + message, file=sys.stderr)
    raise RuntimeError(message)


args = sys.argv[sys.argv.index("--") + 1:]
source_path, output_path, report_path, forward_axis = args

try:
    bpy.ops.import_scene.gltf.get_rna_type()
    bpy.ops.export_scene.gltf.get_rna_type()
except Exception:
    try:
        bpy.ops.preferences.addon_enable(module="io_scene_gltf2")
        bpy.ops.import_scene.gltf.get_rna_type()
        bpy.ops.export_scene.gltf.get_rna_type()
    except Exception as exc:
        fail("Activa la extensión glTF 2.0 de Blender para importar/exportar GLB: " + str(exc))

# Start from an empty scene. Importing/exporting the original remains non-destructive.
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.import_scene.gltf(filepath=source_path)
meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
if not meshes:
    fail("No se encontraron objetos mesh en el GLB.")
if any(obj.type == "ARMATURE" for obj in bpy.context.scene.objects):
    fail("El GLB ya tiene un armature; RigM no lo reemplazará automáticamente.")

# Normalize the chosen front to Blender's conventional -Y so the exported
# character faces the viewer camera consistently. This rotates the whole source
# scene around its horizontal center without changing relative mesh transforms.
input_forward_axis = forward_axis
front_vectors = {"-Y": Vector((0, -1, 0)), "+Y": Vector((0, 1, 0)), "-X": Vector((-1, 0, 0)), "+X": Vector((1, 0, 0))}
front = front_vectors[forward_axis]
target_front = front_vectors["-Y"]
angle = math.atan2(front.x * target_front.y - front.y * target_front.x, front.x * target_front.x + front.y * target_front.y)
initial_points = [obj.matrix_world @ vertex.co for obj in meshes for vertex in obj.data.vertices]
if not initial_points:
    fail("Las mallas no contienen vértices.")
origin = Vector((
    (min(point.x for point in initial_points) + max(point.x for point in initial_points)) * 0.5,
    (min(point.y for point in initial_points) + max(point.y for point in initial_points)) * 0.5,
    0,
))
del initial_points
rotation = Matrix.Rotation(angle, 4, "Z")
transform = Matrix.Translation(origin) @ rotation @ Matrix.Translation(-origin)
if abs(angle) > 1e-6:
    for obj in bpy.context.scene.objects:
        obj.matrix_world = transform @ obj.matrix_world
forward_axis = "-Y"

points = []
for obj in meshes:
    points.extend(obj.matrix_world @ vertex.co for vertex in obj.data.vertices)
if len(points) < 30:
    fail("La malla tiene muy pocos vértices para inferir proporciones humanoides.")

mins = [min(point[i] for point in points) for i in range(3)]
maxs = [max(point[i] for point in points) for i in range(3)]
height = maxs[2] - mins[2]
if height <= 1e-5:
    fail("El modelo no parece estar erguido en el eje Z después de importarlo.")
center = [(mins[i] + maxs[i]) * 0.5 for i in range(3)]
forward_index = {"-Y": 1, "+Y": 1, "-X": 0, "+X": 0}[forward_axis]
side_index = 1 - forward_index
forward_sign = -1 if forward_axis.startswith("-") else 1
# Character-left is up cross forward.
side_sign = (1 if forward_axis == "-Y" else -1 if forward_axis == "+Y" else -1 if forward_axis == "-X" else 1)


def point_at(side, forward, z):
    value = [center[0], center[1], z]
    value[side_index] = center[side_index] + side_sign * side
    value[forward_index] = center[forward_index] + forward_sign * forward
    return Vector(value)


def section(z_ratio):
    z = mins[2] + height * z_ratio
    band = height * 0.012
    values = sorted((point[side_index] - center[side_index]) * side_sign for point in points if abs(point.z - z) <= band)
    if len(values) < 8:
        values = sorted((point[side_index] - center[side_index]) * side_sign for point in points if abs(point.z - z) <= height * 0.035)
    if not values:
        return 0.0, 0.0
    return values[int((len(values) - 1) * 0.1)], values[int((len(values) - 1) * 0.9)]


# Estimate torso width from the narrower cross-sections between waist and chest;
# arm silhouettes can widen shoulder slices and are deliberately excluded.
torso_widths = []
for ratio in (0.53, 0.57, 0.61, 0.65, 0.69):
    lo, hi = section(ratio)
    if hi > lo:
        torso_widths.append(hi - lo)
torso_width = sorted(torso_widths)[len(torso_widths) // 2] if torso_widths else height * 0.2
torso_width = max(height * 0.12, min(height * 0.42, torso_width))
hip_z = mins[2] + height * 0.51
spine_z = mins[2] + height * 0.66
chest_z = mins[2] + height * 0.77
neck_z = mins[2] + height * 0.85
head_z = mins[2] + height * 0.94
top_z = maxs[2] - height * 0.015
shoulder_z = mins[2] + height * 0.77
left_slice, right_slice = section(0.77)
shoulder_half = max(torso_width * 0.56, min(height * 0.34, max(abs(left_slice), abs(right_slice)) * 0.55))

# Find each arm's outside silhouette in the shoulder band. Joints are placed
# along these measured extents, rather than simply scaling all axes by height.
arm_levels = [r / 100 for r in range(56, 86, 2)]
arm_extents = {}
for side_name, sign in (("left", 1), ("right", -1)):
    candidates = []
    for ratio in arm_levels:
        _lo, _hi = section(ratio)
        signed = _hi if sign > 0 else -_lo
        candidates.append((signed, ratio))
    extent, wrist_ratio = max(candidates)
    extent = max(shoulder_half * 1.45, min(height * 0.72, extent))
    arm_extents[side_name] = (extent, mins[2] + height * wrist_ratio)

hip_half = max(height * 0.055, min(torso_width * 0.36, height * 0.17))
knee_half = hip_half * 0.9
ankle_half = hip_half * 0.7
zmin = mins[2]
bones = []


def add_bone(name, head, tail, parent=None):
    if (tail - head).length < height * 0.008:
        tail = head + Vector((0, 0, height * 0.01))
    bones.append((name, head, tail, parent))


pelvis = point_at(0, 0, hip_z)
add_bone("hips", pelvis, point_at(0, 0, spine_z), None)
add_bone("spine", point_at(0, 0, spine_z), point_at(0, 0, chest_z), "hips")
add_bone("chest", point_at(0, 0, chest_z), point_at(0, 0, neck_z), "spine")
add_bone("neck", point_at(0, 0, neck_z), point_at(0, 0, head_z), "chest")
add_bone("head", point_at(0, 0, head_z), point_at(0, 0, top_z), "neck")

for side_name, sign in (("left", 1), ("right", -1)):
    shoulder = point_at(sign * shoulder_half, 0, shoulder_z)
    outside, wrist_z = arm_extents[side_name]
    elbow_side = sign * (shoulder_half + (outside - shoulder_half) * 0.52)
    wrist_side = sign * (shoulder_half + (outside - shoulder_half) * 0.87)
    elbow_z = shoulder_z + (wrist_z - shoulder_z) * 0.52
    wrist_z2 = shoulder_z + (wrist_z - shoulder_z) * 0.88
    elbow = point_at(elbow_side, 0, elbow_z)
    wrist = point_at(wrist_side, 0, wrist_z2)
    hand = point_at(sign * outside, 0, wrist_z)
    add_bone(side_name + "Shoulder", point_at(sign * torso_width * 0.43, 0, chest_z), shoulder, "chest")
    add_bone(side_name + "UpperArm", shoulder, elbow, side_name + "Shoulder")
    add_bone(side_name + "LowerArm", elbow, wrist, side_name + "UpperArm")
    add_bone(side_name + "Hand", wrist, hand, side_name + "LowerArm")
    palm_side = sign * (abs(wrist_side) + (outside - abs(wrist_side)) * 0.42)
    palm_front = height * 0.018
    for finger_name, spread in (("Index", -0.018), ("Middle", -0.006), ("Ring", 0.009), ("Little", 0.024)):
        base = point_at(palm_side + sign * spread * height, palm_front, wrist_z - height * 0.002)
        knuckle = point_at(palm_side + sign * spread * height * 0.8, palm_front + height * 0.018, wrist_z - height * 0.004)
        middle = point_at(palm_side + sign * spread * height * 0.55, palm_front + height * 0.036, wrist_z - height * 0.007)
        tip = point_at(palm_side + sign * spread * height * 0.25, palm_front + height * 0.052, wrist_z - height * 0.01)
        prefix = side_name + finger_name
        add_bone(prefix + "Proximal", base, knuckle, side_name + "Hand")
        add_bone(prefix + "Intermediate", knuckle, middle, prefix + "Proximal")
        add_bone(prefix + "Distal", middle, tip, prefix + "Intermediate")
    thumb_base = point_at(palm_side - sign * height * 0.018, palm_front, wrist_z + height * 0.004)
    thumb_mid = point_at(palm_side - sign * height * 0.036, palm_front + height * 0.018, wrist_z + height * 0.01)
    thumb_tip = point_at(palm_side - sign * height * 0.05, palm_front + height * 0.035, wrist_z + height * 0.016)
    add_bone(side_name + "ThumbMetacarpal", thumb_base, thumb_mid, side_name + "Hand")
    add_bone(side_name + "ThumbProximal", thumb_mid, thumb_tip, side_name + "ThumbMetacarpal")
    add_bone(side_name + "ThumbDistal", thumb_tip, point_at(palm_side - sign * height * 0.055, palm_front + height * 0.045, wrist_z + height * 0.02), side_name + "ThumbProximal")
    hip = point_at(sign * hip_half, 0, hip_z)
    knee = point_at(sign * knee_half, 0, zmin + height * 0.27)
    ankle = point_at(sign * ankle_half, 0, zmin + height * 0.055)
    toe = point_at(sign * ankle_half, -forward_sign * height * 0.12, zmin + height * 0.035)
    add_bone(side_name + "UpperLeg", hip, knee, "hips")
    add_bone(side_name + "LowerLeg", knee, ankle, side_name + "UpperLeg")
    add_bone(side_name + "Foot", ankle, toe, side_name + "LowerLeg")

arm_data = bpy.data.armatures.new("RigM_AutoRig")
arm_obj = bpy.data.objects.new("RigM_AutoRig", arm_data)
bpy.context.collection.objects.link(arm_obj)
bpy.context.view_layer.objects.active = arm_obj
arm_obj.select_set(True)
bpy.ops.object.mode_set(mode="EDIT")
for name, head, tail, parent_name in bones:
    bone = arm_data.edit_bones.new(name)
    bone.head = head
    bone.tail = tail
    if parent_name:
        bone.parent = arm_data.edit_bones[parent_name]
bpy.ops.object.mode_set(mode="OBJECT")
arm_obj.show_in_front = True
arm_obj.display_type = "WIRE"

# Ask Blender's heat-based automatic weights first. A distance-based, normalized
# fallback keeps the result inspectable if Blender reports a heat-weight failure.
bpy.ops.object.select_all(action="DESELECT")
for obj in meshes:
    obj.select_set(True)
arm_obj.select_set(True)
bpy.context.view_layer.objects.active = arm_obj
weights_method = "blender_automatic_weights"
try:
    bpy.ops.object.parent_set(type="ARMATURE_AUTO", keep_transform=True, xmirror=True)
except Exception as exc:
    print("RIGM_AUTORIG_WEIGHT_FALLBACK: " + str(exc), file=sys.stderr)
    weights_method = "normalized_bone_segment_distance"
    segments = [(name, head.copy(), tail.copy()) for name, head, tail, _parent in bones]
    for obj in meshes:
        for group in list(obj.vertex_groups):
            obj.vertex_groups.remove(group)
        groups = {name: obj.vertex_groups.new(name=name) for name, *_ in segments}
        for vertex in obj.data.vertices:
            world = obj.matrix_world @ vertex.co
            distances = []
            for name, start, end in segments:
                direction = end - start
                t = max(0.0, min(1.0, (world - start).dot(direction) / max(direction.length_squared, 1e-12)))
                distances.append(((world - (start + direction * t)).length, name))
            closest = sorted(distances)[:4]
            raw = [(name, math.exp(-((distance / max(height * 0.09, 1e-5)) ** 2) * 2.0)) for distance, name in closest]
            total = sum(weight for _name, weight in raw) or 1.0
            for name, weight in raw:
                groups[name].add([vertex.index], weight / total, "REPLACE")
        if not any(mod.type == "ARMATURE" for mod in obj.modifiers):
            modifier = obj.modifiers.new("RigM AutoRig", "ARMATURE")
            modifier.object = arm_obj

# Export preserves embedded textures/materials and emits a standard glTF skin.
bpy.ops.object.select_all(action="SELECT")
result = bpy.ops.export_scene.gltf(filepath=output_path, export_format="GLB", export_skins=True, export_animations=True)
if "FINISHED" not in result:
    fail("El exportador glTF de Blender no pudo escribir el GLB.")

ratio = maxs[side_index] - mins[side_index]
depth = maxs[forward_index] - mins[forward_index]
shape_ok = 0.16 <= ratio / height <= 0.95 and 0.06 <= depth / height <= 0.75
report = {
    "algorithm": "silhouette_proportions_v1",
    "weighting": weights_method,
    "forward_axis": input_forward_axis,
    "normalized_forward_axis": "-Y",
    "confidence": "medium" if shape_ok else "low",
    "height": round(height, 5),
    "mesh_count": len(meshes),
    "vertex_count": len(points),
    "bone_count": len(bones),
    "landmark_height_ratios": {"pelvis": 0.51, "chest": 0.77, "neck": 0.85, "head": 0.94, "knees": 0.27, "ankles": 0.055},
    "warnings": [
        "El esqueleto se estimó desde la silueta y proporciones globales; revisa pivotes y pesos antes de animar.",
        "La inferencia presupone un humanoide erguido y una malla cerrada; ropa suelta, cabello y accesorios pueden sesgar las medidas.",
        "Las cadenas de dedos son una pose anatómica estándar aproximada; ajusta su orientación y pesos para que sigan la geometría de cada mano.",
    ],
}
if weights_method != "blender_automatic_weights":
    report["warnings"].append("Blender no logró los pesos automáticos por calor; se aplicaron pesos aproximados por distancia a los huesos.")
if not shape_ok:
    report["warnings"].append("Las proporciones están fuera del rango humanoide heurístico; la confianza del auto-rig es baja.")
with open(report_path, "w", encoding="utf-8") as report_file:
    json.dump(report, report_file, ensure_ascii=False, indent=2)
print("RIGM_AUTORIG_COMPLETE: " + json.dumps(report, ensure_ascii=False))
