bl_info = {
    "name": "Animation Video Rendering",
    "author": "Claude",
    "version": (2, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Animation Video Rendering",
    "description": "Batch render animated GLB/FBX files from a folder to video",
    "category": "Render",
}

import bpy
import os
import math
from mathutils import Vector


RESOLUTIONS = [
    ("150", "150p (150x150)", ""),
    ("240", "240p (240x240)", ""),
    ("360", "360p (360x360)", ""),
    ("480", "480p (480x480)", ""),
    ("720", "720p (720x720)", ""),
    ("1080", "1080p (1080x1080)", ""),
    ("1440", "1440p (1440x1440)", ""),
    ("2160", "4K (2160x2160)", ""),
]

RESOLUTION_MAP = {
    "150": (150, 150),
    "240": (240, 240),
    "360": (360, 360),
    "480": (480, 480),
    "720": (720, 720),
    "1080": (1080, 1080),
    "1440": (1440, 1440),
    "2160": (2160, 2160),
}

FORMATS = [
    ("MP4", "MP4 (H.264)", ""),
    ("WEBM", "WebM (VP9)", ""),
]

BG_MODES = [
    ("TRANSPARENT", "Transparent", ""),
    ("SOLID", "Solid Color", ""),
]

RENDER_MODES = [
    ("SELF_CONTAINED", "Self-contained files (mesh + anim)", "Each file has its own mesh and animation"),
    ("RETARGET", "Retarget onto scene armature", "Animation-only files played on the mesh already in the scene"),
]

SOURCE_MODES = [
    ("BATCH", "Batch (folder)", "Render all animation files in a folder"),
    ("SINGLE", "Single file", "Render one animation file"),
]

SUPPORTED_EXTENSIONS = ('.glb', '.gltf', '.fbx')


class GLBBatchRenderProperties(bpy.types.PropertyGroup):
    source_mode: bpy.props.EnumProperty(
        name="Source",
        items=SOURCE_MODES,
        default="BATCH",
    )
    folder_path: bpy.props.StringProperty(
        name="Folder",
        description="Folder containing animation files",
        subtype='DIR_PATH',
        default="",
    )
    single_file: bpy.props.StringProperty(
        name="File",
        description="Single animation file to render",
        subtype='FILE_PATH',
        default="",
    )
    render_mode: bpy.props.EnumProperty(
        name="Mode",
        items=RENDER_MODES,
        default="RETARGET",
    )
    scene_armature: bpy.props.PointerProperty(
        name="Scene Armature",
        description="The armature already in the scene that will receive the animations",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'ARMATURE',
    )
    resolution: bpy.props.EnumProperty(
        name="Resolution",
        items=RESOLUTIONS,
        default="1080",
    )
    output_format: bpy.props.EnumProperty(
        name="Format",
        items=FORMATS,
        default="MP4",
    )
    bg_mode: bpy.props.EnumProperty(
        name="Background",
        items=BG_MODES,
        default="SOLID",
    )
    bg_color: bpy.props.FloatVectorProperty(
        name="Color",
        subtype='COLOR',
        default=(0.05, 0.05, 0.05),
        min=0.0,
        max=1.0,
    )
    fps: bpy.props.IntProperty(
        name="Output FPS",
        description="Output video frame rate. Animations are rescaled from their source FPS to play at correct speed",
        default=30,
        min=1,
        max=120,
    )
    render_thumbnail: bpy.props.BoolProperty(
        name="Render Thumbnail",
        description="Also render a still image at the midpoint of each animation",
        default=True,
    )
    is_rendering: bpy.props.BoolProperty(default=False)


# ---------------------------------------------------------------------------
# Scene helpers
# ---------------------------------------------------------------------------

def remove_objects(objects):
    for obj in list(objects):
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass
    for block_type in [bpy.data.meshes, bpy.data.armatures, bpy.data.cameras,
                       bpy.data.lights, bpy.data.materials, bpy.data.textures,
                       bpy.data.images]:
        for block in list(block_type):
            if block.users == 0:
                block_type.remove(block)


def clear_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=True)
    for block_type in [bpy.data.meshes, bpy.data.materials, bpy.data.textures,
                       bpy.data.images, bpy.data.cameras, bpy.data.lights,
                       bpy.data.armatures, bpy.data.actions, bpy.data.collections]:
        for block in list(block_type):
            if block.users == 0:
                block_type.remove(block)


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

def import_glb(filepath):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=filepath)
    return list(set(bpy.data.objects) - before)


def import_fbx(filepath):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(
        filepath=filepath,
        use_anim=True,
        automatic_bone_orientation=False,
        ignore_leaf_bones=True,
        global_scale=1.0,
    )
    return list(set(bpy.data.objects) - before)


def import_file(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.fbx':
        return import_fbx(filepath)
    return import_glb(filepath)


# ---------------------------------------------------------------------------
# Retargeting helpers
# ---------------------------------------------------------------------------

def find_armature(objects):
    for obj in objects:
        if obj.type == 'ARMATURE':
            return obj
    return None


def _apply_action(armature, action):
    """Assign action to armature, compatible with Blender 4.x and 5.x."""
    if armature.animation_data is None:
        armature.animation_data_create()
    anim_data = armature.animation_data

    if hasattr(anim_data, 'action_slot'):
        anim_data.action = action
        slot = None
        for s in action.slots:
            if s.target_id_type == 'OBJECT':
                slot = s
                break
        if slot is None and action.slots:
            slot = action.slots[0]
        if slot is None:
            try:
                slot = action.slots.new(id_type='OBJECT', name=armature.name)
            except Exception:
                pass
        if slot is not None:
            anim_data.action_slot = slot
    else:
        anim_data.action = action


def _get_fcurves(action):
    """Return all fcurves, compatible with Blender 4.x and 5.x."""
    if hasattr(action, 'layers'):
        fcurves = []
        for layer in action.layers:
            for strip in layer.strips:
                # Blender 5.0+: channelbags is a collection (plural)
                if hasattr(strip, 'channelbags'):
                    for cb in strip.channelbags:
                        fcurves.extend(cb.fcurves)
                # Some builds might use singular channelbag
                elif hasattr(strip, 'channelbag') and strip.channelbag is not None:
                    fcurves.extend(strip.channelbag.fcurves)
                # Fallback
                elif hasattr(strip, 'fcurves'):
                    fcurves.extend(strip.fcurves)
        return fcurves
    return list(action.fcurves)


def _rescale_action_fps(action, source_fps, target_fps):
    """Scale keyframe positions so the animation plays at target_fps
    with correct real-time duration.

    source_fps: effective source FPS (fps / fps_base)
    target_fps: desired output FPS
    """
    if source_fps <= 0 or target_fps <= 0:
        return
    ratio = target_fps / source_fps
    if abs(ratio - 1.0) < 1e-6:
        return

    fcurves = _get_fcurves(action)
    for fc in fcurves:
        for kp in fc.keyframe_points:
            kp.co.x *= ratio
            kp.handle_left.x *= ratio
            kp.handle_right.x *= ratio
        fc.update()

    print(f"    Rescaled action '{action.name}': {source_fps:.2f} -> {target_fps} fps "
          f"(ratio {ratio:.4f}, {len(fcurves)} fcurves)")


def _get_bone_names_from_action(action):
    names = set()
    try:
        fcurves = _get_fcurves(action)
        for fc in fcurves:
            dp = fc.data_path
            if 'pose.bones["' in dp:
                names.add(dp.split('"')[1])
            elif "pose.bones['" in dp:
                names.add(dp.split("'")[1])
    except Exception as e:
        print(f"    _get_bone_names_from_action error: {e}")
    return names


def _find_best_action(target_armature, candidate_actions):
    if not candidate_actions:
        return None
    target_bones = {b.name for b in target_armature.data.bones}
    best_action = None
    best_score = 0
    for action in candidate_actions:
        score = len(_get_bone_names_from_action(action) & target_bones)
        if score > best_score:
            best_score = score
            best_action = action
    return best_action


# ---------------------------------------------------------------------------
# Build action from Empties (for FBX files with no skeleton markers)
# ---------------------------------------------------------------------------

def _create_fcurve_on_action(action, data_path, index, group_name):
    """Create an fcurve on an action, compatible with Blender 4.x and 5.x.

    Blender 5.0+ uses layered actions (no action.fcurves.new).
    We create a layer/strip/channelbag once per action, then reuse it.
    """
    _cache = _fcurve_layer_cache
    cache_key = id(action)

    # Blender 5.0+ layered actions
    if hasattr(action, 'layers'):
        if cache_key not in _cache:
            if not action.layers:
                layer = action.layers.new(name="Layer")
            else:
                layer = action.layers[0]
            if not layer.strips:
                strip = layer.strips.new(type='KEYFRAME')
            else:
                strip = layer.strips[0]
            _cache[cache_key] = (layer, strip)
        else:
            layer, strip = _cache[cache_key]

        # Get or create channelbag for this slot
        slot = None
        if action.slots:
            slot = action.slots[0]
        else:
            try:
                slot = action.slots.new(id_type='OBJECT', name="Armature")
            except Exception:
                slot = action.slots.new()

        channelbag = None
        if hasattr(strip, 'channelbags'):
            for cb in strip.channelbags:
                if cb.slot == slot:
                    channelbag = cb
                    break
            if channelbag is None:
                channelbag = strip.channelbags.new(slot=slot)
        elif hasattr(strip, 'channelbag'):
            channelbag = strip.channelbag

        fc = channelbag.fcurves.new(data_path=data_path, index=index)
        return fc

    # Blender 4.x classic actions
    return action.fcurves.new(
        data_path=data_path, index=index, action_group=group_name
    )


def _count_fcurves_on_action(action):
    """Count fcurves on an action, compatible with Blender 4.x and 5.x."""
    try:
        return len(_get_fcurves(action))
    except Exception:
        pass
    try:
        return len(action.fcurves)
    except Exception:
        return 0


def _build_action_from_empties(imported_objects, target_armature, action_name):
    """When FBX has no Skeleton/LimbNode markers, Blender imports 'bones' as
    Empty objects with their own individual animation_data.

    This bakes the empties' world-space transforms into correct pose-bone-local
    transforms by sampling every frame.
    """
    target_bone_names = {b.name for b in target_armature.data.bones}
    target_by_lower = {b.lower(): b for b in target_bone_names}

    # Match empties to bones
    matching = []
    for obj in imported_objects:
        if obj.type != 'EMPTY' or obj.animation_data is None:
            continue
        if obj.animation_data.action is None:
            continue

        bone_name = None
        name = obj.name
        if name in target_bone_names:
            bone_name = name
        elif name.lower() in target_by_lower:
            bone_name = target_by_lower[name.lower()]
        else:
            base = name.rsplit('.', 1)[0]
            if base in target_bone_names:
                bone_name = base
            elif base.lower() in target_by_lower:
                bone_name = target_by_lower[base.lower()]

        if bone_name:
            matching.append((obj, bone_name))

    if not matching:
        animated_empties = [o for o in imported_objects
                           if o.type == 'EMPTY' and o.animation_data
                           and o.animation_data.action]
        if animated_empties:
            print(f"    Found {len(animated_empties)} animated Empties but none match target bones")
            print(f"    Empty names: {[o.name for o in animated_empties[:5]]}")
            print(f"    Target bones: {sorted(target_bone_names)[:5]}")
        return None

    print(f"    Building action from {len(matching)} animated Empties (bake method)")

    # Find the frame range across all animated empties
    frame_min = float('inf')
    frame_max = float('-inf')
    for obj, _ in matching:
        action = obj.animation_data.action
        try:
            fr = action.frame_range
            frame_min = min(frame_min, fr[0])
            frame_max = max(frame_max, fr[1])
        except Exception:
            pass

    if frame_min == float('inf'):
        return None

    frame_start = int(frame_min)
    frame_end = int(frame_max)
    print(f"    Frame range: {frame_start}-{frame_end}")

    # Build a lookup: bone_name -> empty object
    empty_by_bone = {bone_name: obj for obj, bone_name in matching}

    # Switch bones to euler rotation mode
    for pb in target_armature.pose.bones:
        pb.rotation_mode = 'XYZ'

    scene = bpy.context.scene
    orig_frame = scene.frame_current

    # Sample each frame: read empty transforms, compute pose bone transforms
    # Store: bone_name -> { frame -> (loc, rot_euler) }
    sampled = {bone_name: {} for _, bone_name in matching}

    for frame in range(frame_start, frame_end + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()

        for bone_name, empty_obj in empty_by_bone.items():
            pose_bone = target_armature.pose.bones.get(bone_name)
            if pose_bone is None:
                continue

            # Get the empty's world matrix
            empty_world = empty_obj.matrix_world

            # Get the armature's world matrix
            arm_world = target_armature.matrix_world
            arm_world_inv = arm_world.inverted()

            # Convert empty's world transform to armature-local space
            empty_in_arm = arm_world_inv @ empty_world

            # Get the bone's rest matrix in armature space
            rest_matrix = pose_bone.bone.matrix_local

            # If bone has a parent, factor out parent's rest transform
            if pose_bone.parent:
                parent_rest = pose_bone.parent.bone.matrix_local
                parent_rest_inv = parent_rest.inverted()
                rest_in_parent = parent_rest_inv @ rest_matrix
                # The empty hierarchy mirrors the bone hierarchy
                # Get the parent empty's transform
                parent_bone_name = pose_bone.parent.bone.name
                parent_empty = empty_by_bone.get(parent_bone_name)
                if parent_empty:
                    parent_world = parent_empty.matrix_world
                    parent_in_arm = arm_world_inv @ parent_world
                    parent_in_arm_inv = parent_in_arm.inverted()
                    # Empty's transform relative to its parent empty
                    empty_local = parent_in_arm_inv @ empty_in_arm
                else:
                    empty_local = empty_in_arm
            else:
                empty_local = empty_in_arm
                rest_in_parent = rest_matrix

            # The pose transform = rest_inverse @ empty_local
            rest_inv = rest_in_parent.inverted()
            pose_matrix = rest_inv @ empty_local

            loc = pose_matrix.to_translation()
            rot = pose_matrix.to_euler('XYZ')

            sampled[bone_name][frame] = (loc, rot)

    scene.frame_set(orig_frame)

    # Create the action and write keyframes
    new_action = bpy.data.actions.new(name=action_name)
    _fcurve_layer_cache.pop(id(new_action), None)

    fc_count = 0
    for bone_name, frames_data in sampled.items():
        if not frames_data:
            continue

        # Create fcurves for location (3) and rotation_euler (3)
        loc_fcs = []
        rot_fcs = []
        for i in range(3):
            try:
                lfc = _create_fcurve_on_action(
                    new_action,
                    f'pose.bones["{bone_name}"].location',
                    i, bone_name
                )
                loc_fcs.append(lfc)
                fc_count += 1
            except Exception:
                loc_fcs.append(None)

            try:
                rfc = _create_fcurve_on_action(
                    new_action,
                    f'pose.bones["{bone_name}"].rotation_euler',
                    i, bone_name
                )
                rot_fcs.append(rfc)
                fc_count += 1
            except Exception:
                rot_fcs.append(None)

        for frame in sorted(frames_data.keys()):
            loc, rot = frames_data[frame]
            for i in range(3):
                if loc_fcs[i]:
                    kp = loc_fcs[i].keyframe_points.insert(
                        frame=frame, value=loc[i], options={'FAST'})
                    kp.interpolation = 'LINEAR'
                if rot_fcs[i]:
                    kp = rot_fcs[i].keyframe_points.insert(
                        frame=frame, value=rot[i], options={'FAST'})
                    kp.interpolation = 'LINEAR'

        for fc in loc_fcs + rot_fcs:
            if fc:
                fc.update()

    _fcurve_layer_cache.pop(id(new_action), None)

    if fc_count == 0:
        bpy.data.actions.remove(new_action)
        return None

    try:
        new_action.frame_range = (frame_start, frame_end)
    except Exception:
        pass

    rebuilt_bones = _get_bone_names_from_action(new_action)
    print(f"    Rebuilt: {fc_count} fcurves, "
          f"{len(rebuilt_bones)} bones, frames {frame_start}-{frame_end}")
    return new_action


# ---------------------------------------------------------------------------
# Main retarget function
# ---------------------------------------------------------------------------

def retarget_onto_armature(imported_objects, target_armature, new_actions=None,
                           action_name="imported_anim"):
    """Try multiple strategies to get an animation onto the target armature:
    1. Direct action from an imported armature with matching bone names
    2. Best matching new action with pose.bones paths
    3. Rebuild from animated Empties (FBX with no skeleton markers)
    4. Brute-force: just apply any action that has fcurves (trust it)
    """
    target_bones = {b.name for b in target_armature.data.bones}
    print(f"    Target armature '{target_armature.name}' has {len(target_bones)} bones")

    # Diagnostic: what did we import?
    imported_types = {}
    for o in imported_objects:
        imported_types[o.type] = imported_types.get(o.type, 0) + 1
    print(f"    Imported object types: {imported_types}")

    # Strategy 1: imported armature's action
    source_arm = find_armature(imported_objects)
    action = None
    if source_arm:
        print(f"    Found imported armature: '{source_arm.name}'")
        if source_arm.animation_data and source_arm.animation_data.action:
            candidate = source_arm.animation_data.action
            bone_names = _get_bone_names_from_action(candidate)
            overlap = len(bone_names & target_bones)
            print(f"    Strategy 1: action '{candidate.name}' has {len(bone_names)} bone channels, {overlap} match target")
            if overlap > 0:
                action = candidate
                print(f"    -> Using Strategy 1")
            elif len(bone_names) == 0:
                # fcurves might exist but _get_bone_names couldn't parse them
                # Try it anyway as Strategy 4 fallback
                try:
                    fc_count = len(_get_fcurves(candidate))
                except Exception:
                    fc_count = 0
                print(f"    Strategy 1: 0 bone names parsed but {fc_count} fcurves exist")
        else:
            print(f"    Imported armature has no animation_data or action")
    else:
        print(f"    No imported armature found")

    # Strategy 2: best new action with pose.bones paths
    if action is None and new_actions:
        print(f"    Checking {len(new_actions)} new action(s)...")
        for i, na in enumerate(new_actions):
            bone_names = _get_bone_names_from_action(na)
            try:
                fc_count = len(_get_fcurves(na))
            except Exception:
                fc_count = 0
            overlap = len(bone_names & target_bones)
            print(f"    Action[{i}] '{na.name}': {fc_count} fcurves, {len(bone_names)} bones, {overlap} match")
            if overlap > 0 and (action is None):
                action = na
                print(f"    -> Using Strategy 2")

    # Strategy 3: rebuild from animated Empties
    if action is None:
        animated_empties = [o for o in imported_objects
                           if o.type == 'EMPTY' and o.animation_data
                           and o.animation_data.action]
        print(f"    Strategy 3: found {len(animated_empties)} animated Empties")
        if animated_empties:
            # Show names for debugging
            sample = [o.name for o in animated_empties[:8]]
            print(f"    Empty names: {sample}")
            action = _build_action_from_empties(imported_objects, target_armature, action_name)
            if action:
                print(f"    -> Using Strategy 3")

    # Strategy 4: brute-force - if we found an imported armature, just try
    # applying its action directly even if we couldn't parse the bone names.
    # Also try any new action that has fcurves.
    if action is None:
        print(f"    Strategy 4: brute-force attempt...")
        candidates = []
        if source_arm and source_arm.animation_data and source_arm.animation_data.action:
            candidates.append(source_arm.animation_data.action)
        if new_actions:
            candidates.extend(new_actions)
        for c in candidates:
            try:
                fc_count = len(_get_fcurves(c))
            except Exception:
                fc_count = 0
            if fc_count > 0:
                action = c
                print(f"    -> Using Strategy 4: '{c.name}' with {fc_count} fcurves")
                break
        # Last resort: any new action at all
        if action is None and new_actions:
            action = new_actions[0]
            print(f"    -> Using Strategy 4 last resort: '{action.name}'")

    if action is None:
        print(f"    ALL strategies failed - no action found")
        return None

    _apply_action(target_armature, action)

    # Force pose evaluation
    try:
        fr = action.frame_range
        bpy.context.scene.frame_set(int(fr[0]))
    except Exception:
        bpy.context.scene.frame_set(1)
    bpy.context.view_layer.update()

    return action


# ---------------------------------------------------------------------------
# Camera & lighting
# ---------------------------------------------------------------------------

def get_scene_bounds(objects):
    mn = Vector((float('inf'),) * 3)
    mx = Vector((float('-inf'),) * 3)
    for obj in objects:
        if obj.type == 'ARMATURE':
            check = [c for c in obj.children if c.type == 'MESH']
        elif obj.type == 'MESH':
            check = [obj]
        else:
            continue
        for mesh_obj in check:
            for c in [mesh_obj.matrix_world @ Vector(corner) for corner in mesh_obj.bound_box]:
                mn = Vector((min(mn.x, c.x), min(mn.y, c.y), min(mn.z, c.z)))
                mx = Vector((max(mx.x, c.x), max(mx.y, c.y), max(mx.z, c.z)))
    if mn.x == float('inf'):
        return Vector((0, 0, 0)), Vector((1, 1, 1))
    return mn, mx


def get_animated_bounds(armature, frame_start, frame_end, sample_step=5):
    scene = bpy.context.scene
    mn = Vector((float('inf'),) * 3)
    mx = Vector((float('-inf'),) * 3)
    orig = scene.frame_current
    meshes = [c for c in armature.children if c.type == 'MESH']
    if not meshes:
        scene.frame_set(orig)
        return get_scene_bounds([armature])

    for frame in range(int(frame_start), int(frame_end) + 1, sample_step):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        for mo in meshes:
            eo = mo.evaluated_get(dg)
            if eo.data and hasattr(eo.data, 'vertices') and len(eo.data.vertices) > 0:
                for v in eo.data.vertices:
                    wc = mo.matrix_world @ v.co
                    mn = Vector((min(mn.x, wc.x), min(mn.y, wc.y), min(mn.z, wc.z)))
                    mx = Vector((max(mx.x, wc.x), max(mx.y, wc.y), max(mx.z, wc.z)))
            else:
                for corner in mo.bound_box:
                    c = mo.matrix_world @ Vector(corner)
                    mn = Vector((min(mn.x, c.x), min(mn.y, c.y), min(mn.z, c.z)))
                    mx = Vector((max(mx.x, c.x), max(mx.y, c.y), max(mx.z, c.z)))
    scene.frame_set(orig)
    if mn.x == float('inf'):
        return Vector((0, 0, 0)), Vector((2, 2, 2))
    margin = (mx - mn) * 0.1
    return mn - margin, mx + margin


def setup_camera_from_viewport():
    """Create a camera matching the viewport view."""
    viewport = None
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            viewport = area
            break

    if viewport is None:
        return None

    space = None
    for s in viewport.spaces:
        if s.type == 'VIEW_3D':
            space = s
            break

    if space is None:
        return None

    cam_data = bpy.data.cameras.new("BatchRenderCam")
    cam_data.lens = space.lens
    cam_obj = bpy.data.objects.new("BatchRenderCam", cam_data)
    bpy.context.scene.collection.objects.link(cam_obj)
    bpy.context.scene.camera = cam_obj

    for region in viewport.regions:
        if region.type == 'WINDOW':
            try:
                with bpy.context.temp_override(area=viewport, region=region):
                    bpy.ops.view3d.camera_to_view()
            except Exception:
                cam_obj.matrix_world = space.region_3d.view_matrix.inverted()

            # Set sensor_fit to VERTICAL so that switching to 1:1 aspect
            # crops the sides rather than zooming out to show more vertically
            cam_data.sensor_fit = 'VERTICAL'

            # camera_to_view crops to ~50% of what viewport shows
            # Halve the lens to widen the FOV and match
            cam_data.lens = space.lens * 0.5
            print(f"    Camera from viewport: original lens={space.lens}, adjusted={cam_data.lens:.1f}")
            return cam_obj

    return None


def setup_camera(objects, use_animation_bounds=False, armature=None,
                 frame_start=None, frame_end=None):
    """Use scene camera if one exists, otherwise try viewport, then compute."""

    # 1. Scene already has a render camera assigned — use it as-is.
    existing = bpy.context.scene.camera
    if existing is not None and existing.type == 'CAMERA':
        print(f"    Using existing scene camera: '{existing.name}'")
        return existing

    # 2. Any camera object present in the scene — make it the render camera.
    all_scene_objs = set(bpy.context.scene.collection.all_objects)
    for obj in bpy.data.objects:
        if obj.type == 'CAMERA' and obj in all_scene_objs:
            bpy.context.scene.camera = obj
            print(f"    Found camera in scene, set as render camera: '{obj.name}'")
            return obj

    # 3. No camera in scene — try to create one from the current viewport view.
    cam = setup_camera_from_viewport()
    if cam is not None:
        return cam

    # 4. Fallback: compute camera position from object bounds.
    if use_animation_bounds and armature is not None:
        mn, mx = get_animated_bounds(armature, frame_start, frame_end)
    else:
        mn, mx = get_scene_bounds(objects)
    size = mx - mn
    center = (mn + mx) / 2
    max_dim = max(size.x, size.y, size.z, 0.01)
    cam_data = bpy.data.cameras.new("BatchRenderCam")
    cam_data.lens = 35
    cam_obj = bpy.data.objects.new("BatchRenderCam", cam_data)
    bpy.context.scene.collection.objects.link(cam_obj)
    distance = max_dim * 2.8
    cam_obj.location = Vector((center.x, center.y - distance, center.z))
    cam_obj.rotation_euler = (math.radians(90), 0, 0)
    bpy.context.scene.camera = cam_obj
    print(f"    Created computed camera (no scene camera found)")
    return cam_obj


def setup_lighting(objects):
    mn, mx = get_scene_bounds(objects)
    center = (mn + mx) / 2
    max_dim = max(mx - mn)
    dist = max_dim * 2.5

    def make_light(name, kind, energy_factor, size_factor, loc):
        data = bpy.data.lights.new(name, kind)
        data.energy = max_dim * energy_factor
        data.size = max_dim * size_factor
        obj = bpy.data.objects.new(name, data)
        obj.location = loc
        obj.rotation_euler = (center - obj.location).to_track_quat('-Z', 'Y').to_euler()
        bpy.context.scene.collection.objects.link(obj)
        return obj

    return [
        make_light("KeyLight",  'AREA', 150, 1.0, (center.x - dist * 0.7, center.y - dist,       center.z + dist * 0.8)),
        make_light("FillLight", 'AREA',  80, 1.5, (center.x + dist * 0.8, center.y - dist * 0.6, center.z + dist * 0.4)),
        make_light("BackLight", 'AREA', 100, 1.0, (center.x,              center.y + dist * 0.8,  center.z + dist)),
    ]


# ---------------------------------------------------------------------------
# Render configuration
# ---------------------------------------------------------------------------

def detect_animation_range(action=None):
    if action is not None:
        return int(action.frame_range[0]), int(action.frame_range[1])
    start, end = float('inf'), float('-inf')
    for act in bpy.data.actions:
        start = min(start, act.frame_range[0])
        end   = max(end,   act.frame_range[1])
    if start == float('inf'):
        return 1, 250
    return int(start), int(end)


def configure_render(props, output_path, fps_override=None, fps_base_override=1):
    scene = bpy.context.scene
    render = scene.render
    w, h = RESOLUTION_MAP[props.resolution]
    render.resolution_x = w
    render.resolution_y = h
    render.resolution_percentage = 100
    render.pixel_aspect_x = 1.0
    render.pixel_aspect_y = 1.0
    if fps_override is not None:
        scene.render.fps = fps_override
        scene.render.fps_base = fps_base_override
        print(f"    Render FPS: {fps_override}/{fps_base_override} (detected from source)")
    else:
        scene.render.fps = props.fps
        scene.render.fps_base = 1
        print(f"    Render FPS: {props.fps} (from settings)")
    print(f"    Render resolution: {w}x{h}")

    if bpy.app.version >= (5, 0, 0):
        render.engine = 'BLENDER_EEVEE'
    elif bpy.app.version >= (4, 0, 0):
        render.engine = 'BLENDER_EEVEE_NEXT'
    else:
        render.engine = 'BLENDER_EEVEE'

    render.film_transparent = (props.bg_mode == 'TRANSPARENT')
    world = bpy.data.worlds.new("BatchRenderWorld")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        if props.bg_mode == 'TRANSPARENT':
            bg.inputs[0].default_value = (0, 0, 0, 1)
        else:
            r, g, b = props.bg_color
            bg.inputs[0].default_value = (r, g, b, 1)

    if hasattr(render.image_settings, 'media_type'):
        render.image_settings.media_type = 'VIDEO'
    render.image_settings.file_format = 'FFMPEG'

    if props.output_format == 'MP4':
        render.ffmpeg.format = 'MPEG4'
        render.ffmpeg.codec = 'H264'
        ext = ".mp4"
    else:
        render.ffmpeg.format = 'WEBM'
        render.ffmpeg.codec = 'WEBM'
        ext = ".webm"

    render.ffmpeg.constant_rate_factor = 'MEDIUM'
    render.ffmpeg.audio_codec = 'NONE'
    render.filepath = output_path + ext


# ---------------------------------------------------------------------------
# Modal batch render operator
# ---------------------------------------------------------------------------

# Module-level render state (avoids StructRNA crash when operator is GC'd)
_render_state = {'count': 0, 'cancelled': False}

# Cache for _create_fcurve_on_action: action id -> (layer, strip)
# Kept at module level to avoid mutable-default-argument pitfalls.
_fcurve_layer_cache = {}


def _on_render_complete(scene, depsgraph=None):
    _render_state['count'] += 1


def _on_render_cancel(scene, depsgraph=None):
    _render_state['count'] += 1


class GLB_OT_batch_render(bpy.types.Operator):
    bl_idname = "glb_batch.render"
    bl_label = "Render All Animations"
    bl_description = "Batch render animation files non-blocking (modal)"
    bl_options = {'REGISTER'}

    _timer = None
    _files: list = []
    _index: int = 0
    _total: int = 0
    _last_imported: list = []
    _last_rebuilt_action = None
    _rendering: bool = False
    _render_count_before: int = 0
    _pending_thumbnail: str = ""
    _thumbnail_frame: int = 1
    _props_snapshot: dict = {}
    _renders_dir: str = ""
    _retarget_mode: bool = False
    _target_armature = None
    _persistent_objects: list = []

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        props = context.scene.glb_batch_render

        # Gather files based on source mode
        if props.source_mode == 'SINGLE':
            filepath = bpy.path.abspath(props.single_file)
            if not filepath or not os.path.isfile(filepath):
                self.report({'ERROR'}, f"Invalid file: {filepath}")
                return {'CANCELLED'}
            ext = os.path.splitext(filepath)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                self.report({'ERROR'}, f"Unsupported file type: {ext}")
                return {'CANCELLED'}
            folder = os.path.dirname(filepath)
            filename = os.path.basename(filepath)
            files = [filename]
        else:
            folder = bpy.path.abspath(props.folder_path)
            if not folder or not os.path.isdir(folder):
                self.report({'ERROR'}, f"Invalid folder: {folder}")
                return {'CANCELLED'}
            files = sorted([f for f in os.listdir(folder) if f.lower().endswith(SUPPORTED_EXTENSIONS)])
            if not files:
                self.report({'ERROR'}, "No GLB/glTF/FBX files found in folder")
                return {'CANCELLED'}

        self._retarget_mode = (props.render_mode == 'RETARGET')
        if self._retarget_mode:
            if props.scene_armature is None:
                self.report({'ERROR'}, "Retarget mode: please select the Scene Armature")
                return {'CANCELLED'}
            self._target_armature = props.scene_armature
            self._persistent_objects = [o for o in bpy.data.objects if o.type in ('MESH', 'ARMATURE')]
        else:
            self._target_armature = None
            self._persistent_objects = []

        renders_dir = os.path.join(folder, "renders")
        os.makedirs(renders_dir, exist_ok=True)

        self._props_snapshot = {
            'folder': folder,
            'resolution': props.resolution,
            'output_format': props.output_format,
            'bg_mode': props.bg_mode,
            'bg_color': tuple(props.bg_color),
            'fps': props.fps,
            'render_thumbnail': props.render_thumbnail,
        }
        self._files = [(f, os.path.join(folder, f)) for f in files]
        self._renders_dir = renders_dir
        self._index = 0
        self._total = len(files)
        self._last_imported = []
        self._last_rebuilt_action = None
        self._rendering = False

        if self._retarget_mode:
            if bpy.context.scene.camera is None:
                setup_camera(self._persistent_objects)

        _render_state['cancelled'] = False
        props.is_rendering = True
        bpy.app.handlers.render_complete.append(_on_render_complete)
        bpy.app.handlers.render_cancel.append(_on_render_cancel)
        self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        # Waiting for a video render to finish
        if self._rendering:
            if _render_state['count'] > self._render_count_before:
                self._rendering = False

                # If thumbnails enabled, render one before advancing
                if self._props_snapshot.get('render_thumbnail', False) and self._pending_thumbnail:
                    scene = bpy.context.scene
                    scene.frame_set(self._thumbnail_frame)
                    bpy.context.view_layer.update()

                    render = scene.render
                    if hasattr(render.image_settings, 'media_type'):
                        render.image_settings.media_type = 'IMAGE'
                    render.image_settings.file_format = 'PNG'
                    render.filepath = self._pending_thumbnail

                    print(f"    Rendering thumbnail at frame {self._thumbnail_frame}")
                    try:
                        bpy.ops.render.render(write_still=True)
                    except Exception as e:
                        print(f"    Thumbnail render failed: {e}")
                    print(f"    Thumbnail saved")

                self._index += 1
            else:
                return {'RUNNING_MODAL'}

        if self._index >= self._total:
            return self._finish(context, cancelled=False)

        if _render_state.get('cancelled'):
            return self._finish(context, cancelled=True)

        filename, filepath = self._files[self._index]
        name_no_ext = os.path.splitext(filename)[0]
        output_path = os.path.join(self._renders_dir, name_no_ext)

        print(f"\n{'='*60}")
        print(f"AnimVideo: [{self._index + 1}/{self._total}] {filename}")
        print(f"{'='*60}")

        # Defaults — overridden after import if source FPS was detected
        detected_fps = self._props_snapshot['fps']
        detected_fps_base = 1

        if self._retarget_mode:
            # Clear previous action
            if self._target_armature and self._target_armature.animation_data:
                self._target_armature.animation_data.action = None

            # Remove rebuilt action
            if self._last_rebuilt_action is not None:
                try:
                    bpy.data.actions.remove(self._last_rebuilt_action)
                except Exception:
                    pass
                self._last_rebuilt_action = None

            # Clean up previous import
            if self._last_imported:
                remove_objects(self._last_imported)
                self._last_imported = []

            for a in list(bpy.data.actions):
                if a.users == 0:
                    bpy.data.actions.remove(a)

            actions_before = set(bpy.data.actions)

            # Set fps before import so GLB bakes at the right rate.
            # FBX importer will override this with the file's own fps.
            bpy.context.scene.render.fps = self._props_snapshot['fps']
            bpy.context.scene.render.fps_base = 1

            try:
                imported = import_file(filepath)
            except Exception as e:
                self.report({'WARNING'}, f"Import failed {filename}: {e}")
                self._index += 1
                return {'RUNNING_MODAL'}

            detected_fps = bpy.context.scene.render.fps
            detected_fps_base = bpy.context.scene.render.fps_base

            self._last_imported = imported
            new_actions = list(set(bpy.data.actions) - actions_before)

            types = {}
            for o in imported:
                types[o.type] = types.get(o.type, 0) + 1
            print(f"  Imported: {types}, new actions: {len(new_actions)}")

            action = retarget_onto_armature(
                imported, self._target_armature,
                new_actions=new_actions, action_name=name_no_ext,
            )
            if action is None:
                self.report({'WARNING'}, f"No animation in {filename}, skipping")
                self._index += 1
                return {'RUNNING_MODAL'}

            if action not in new_actions:
                self._last_rebuilt_action = action

            # Hide ALL imported objects so only the scene basemesh renders
            for obj in imported:
                obj.hide_render = True
                obj.hide_viewport = True
                obj.hide_set(True)
                # Also hide any children that came along
                for child in obj.children_recursive:
                    child.hide_render = True
                    child.hide_viewport = True
                    child.hide_set(True)

            # Detect frame range BEFORE rescaling (action.frame_range may
            # not refresh after direct keyframe edits).
            frame_start, frame_end = detect_animation_range(action)

            # Rescale keyframes if source FPS differs from desired output FPS
            source_effective_fps = detected_fps / detected_fps_base
            target_fps = self._props_snapshot['fps']
            if abs(source_effective_fps - target_fps) > 0.01:
                ratio = target_fps / source_effective_fps
                _rescale_action_fps(action, source_effective_fps, target_fps)
                frame_start = int(round(frame_start * ratio))
                frame_end = int(math.ceil(frame_end * ratio))
                detected_fps = target_fps
                detected_fps_base = 1

            bpy.context.scene.frame_start = frame_start
            bpy.context.scene.frame_end = frame_end

        else:
            clear_scene()

            # Set fps before import so GLB bakes at the right rate.
            # FBX importer will override this with the file's own fps.
            bpy.context.scene.render.fps = self._props_snapshot['fps']
            bpy.context.scene.render.fps_base = 1

            try:
                imported = import_file(filepath)
            except Exception as e:
                self.report({'WARNING'}, f"Import failed {filename}: {e}")
                self._index += 1
                return {'RUNNING_MODAL'}

            detected_fps = bpy.context.scene.render.fps
            detected_fps_base = bpy.context.scene.render.fps_base

            if not imported:
                self.report({'WARNING'}, f"Nothing imported from {filename}")
                self._index += 1
                return {'RUNNING_MODAL'}

            if bpy.context.scene.camera is None:
                setup_camera(imported)

            # Detect frame range BEFORE rescaling
            frame_start, frame_end = detect_animation_range()

            # Rescale ALL actions if source FPS differs from desired output FPS
            source_effective_fps = detected_fps / detected_fps_base
            target_fps = self._props_snapshot['fps']
            if abs(source_effective_fps - target_fps) > 0.01:
                ratio = target_fps / source_effective_fps
                for act in bpy.data.actions:
                    _rescale_action_fps(act, source_effective_fps, target_fps)
                frame_start = int(round(frame_start * ratio))
                frame_end = int(math.ceil(frame_end * ratio))
                detected_fps = target_fps
                detected_fps_base = 1

            bpy.context.scene.frame_start = frame_start
            bpy.context.scene.frame_end = frame_end

        # Render
        class _P:
            pass
        p = _P()
        s = self._props_snapshot
        p.resolution = s['resolution']
        p.output_format = s['output_format']
        p.bg_mode = s['bg_mode']
        p.bg_color = s['bg_color']
        p.fps = s['fps']
        configure_render(p, output_path,
                         fps_override=detected_fps,
                         fps_base_override=detected_fps_base)

        # Set up thumbnail info for after video completes
        mid_frame = (bpy.context.scene.frame_start + bpy.context.scene.frame_end) // 2
        self._thumbnail_frame = mid_frame
        self._pending_thumbnail = os.path.join(self._renders_dir, name_no_ext + "_thumb.png")

        self.report({'INFO'}, f"Rendering [{self._index+1}/{self._total}]: {filename}")
        self._render_count_before = _render_state['count']
        self._rendering = True

        try:
            bpy.ops.render.render('INVOKE_DEFAULT', animation=True)
        except Exception as e:
            self.report({'WARNING'}, f"Render failed {filename}: {e}")
            self._rendering = False
            self._index += 1

        return {'RUNNING_MODAL'}

    def _finish(self, context, cancelled=False):
        context.window_manager.event_timer_remove(self._timer)
        # Clean up handlers
        if _on_render_complete in bpy.app.handlers.render_complete:
            bpy.app.handlers.render_complete.remove(_on_render_complete)
        if _on_render_cancel in bpy.app.handlers.render_cancel:
            bpy.app.handlers.render_cancel.remove(_on_render_cancel)

        if self._retarget_mode:
            if self._last_imported:
                remove_objects(self._last_imported)
            if self._last_rebuilt_action:
                try:
                    bpy.data.actions.remove(self._last_rebuilt_action)
                except Exception:
                    pass
            if self._target_armature and self._target_armature.animation_data:
                self._target_armature.animation_data.action = None

        context.scene.glb_batch_render.is_rendering = False
        if cancelled:
            self.report({'WARNING'}, "Batch render cancelled.")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Done! {self._total} file(s) rendered.")
        return {'FINISHED'}


class GLB_OT_cancel_render(bpy.types.Operator):
    bl_idname = "glb_batch.cancel_render"
    bl_label = "Cancel Render"

    def execute(self, context):
        _render_state['cancelled'] = True
        bpy.ops.render.render('INVOKE_DEFAULT')
        self.report({'INFO'}, "Cancelling...")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class GLB_PT_batch_render_panel(bpy.types.Panel):
    bl_label = "Animation Video Rendering"
    bl_idname = "GLB_PT_batch_render_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Animation Video Rendering"

    def draw(self, context):
        layout = self.layout
        props = context.scene.glb_batch_render

        if props.is_rendering:
            layout.label(text="Rendering in progress...", icon='RENDER_ANIMATION')
            layout.operator("glb_batch.cancel_render", icon='CANCEL')
            return

        # Source selection
        layout.label(text="Source:")
        layout.prop(props, "source_mode", text="")
        if props.source_mode == 'BATCH':
            layout.prop(props, "folder_path")
        else:
            layout.prop(props, "single_file")

        layout.separator()
        layout.label(text="Mode:")
        layout.prop(props, "render_mode", text="")
        if props.render_mode == 'RETARGET':
            box = layout.box()
            box.label(text="Scene Armature (receives animation):")
            box.prop(props, "scene_armature", text="")
            if props.scene_armature is None:
                box.label(text="Select the armature in your scene", icon='ERROR')

        layout.separator()
        layout.label(text="Render Settings:")
        layout.prop(props, "resolution")
        layout.prop(props, "output_format")
        layout.prop(props, "fps")
        layout.prop(props, "render_thumbnail")

        layout.separator()
        layout.label(text="Background:")
        layout.prop(props, "bg_mode", text="")
        if props.bg_mode == 'SOLID':
            layout.prop(props, "bg_color", text="")

        layout.separator()
        if props.source_mode == 'BATCH':
            folder = bpy.path.abspath(props.folder_path)
            if folder and os.path.isdir(folder):
                files = [f for f in os.listdir(folder) if f.lower().endswith(SUPPORTED_EXTENSIONS)]
                layout.label(text=f"Found {len(files)} animation file(s)")
        else:
            filepath = bpy.path.abspath(props.single_file)
            if filepath and os.path.isfile(filepath):
                layout.label(text=f"File: {os.path.basename(filepath)}")

        layout.separator()
        layout.operator("glb_batch.render", icon='RENDER_ANIMATION')


classes = (
    GLBBatchRenderProperties,
    GLB_OT_batch_render,
    GLB_OT_cancel_render,
    GLB_PT_batch_render_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.glb_batch_render = bpy.props.PointerProperty(type=GLBBatchRenderProperties)


def unregister():
    del bpy.types.Scene.glb_batch_render
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
