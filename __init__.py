bl_info = {
    "name": "Face Keys",
    "author": "yakuruto",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > Face Keys",
    "description": "Shape key sliders in the viewport sidebar, a 2D eye look pad and auto detection",
    "category": "Animation",
}

if "bpy" in locals():
    import importlib
    importlib.reload(detect)  # noqa: F821
else:
    from . import detect

import blf
import bpy
import gpu
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty,
                       PointerProperty, StringProperty)
from bpy.types import Operator, Panel, PropertyGroup, UIList
from gpu_extras.batch import batch_for_shader

LOOK_DIRS = ("up", "down", "left", "right")
LOOK_AXES = ("vert", "horz")
AXIS_LABEL = {"vert": "Up/Down", "horz": "Left/Right"}
ROLES = [
    ("CUSTOM", "Custom", ""),
    ("BLINK", "Blink", "Both eyes closed"),
    ("BLINK_L", "Blink L", "Left eye closed"),
    ("BLINK_R", "Blink R", "Right eye closed"),
    ("MOUTH_OPEN", "Mouth Open", ""),
]
ROLE_LABEL = {"BLINK": "Blink", "BLINK_L": "Blink L", "BLINK_R": "Blink R", "MOUTH_OPEN": "Mouth Open"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def settings(context):
    return context.scene.face_keys


def shape_key_data(s):
    """(object, Key datablock) of the target mesh, or (object, None)."""
    ob = s.target
    if ob is None or ob.type != "MESH" or ob.data.shape_keys is None:
        return ob, None
    return ob, ob.data.shape_keys


def value_path(name):
    return 'key_blocks["%s"].value' % bpy.utils.escape_identifier(name)


def _fcurves(key):
    ad = key.animation_data
    if ad is None or ad.action is None:
        return []
    act = ad.action
    layers = getattr(act, "layers", None)
    if layers:
        slot = getattr(ad, "action_slot", None)
        out = []
        for layer in layers:
            for strip in layer.strips:
                for cb in getattr(strip, "channelbags", []):
                    if slot is None or cb.slot == slot:
                        out.extend(cb.fcurves)
        return out
    return list(getattr(act, "fcurves", []))


def key_state(key, name, frame):
    """(keyed on this frame, value differs from its animation = an unkeyed change)."""
    path = value_path(name)
    kb = key.key_blocks.get(name)
    for fc in _fcurves(key):
        if fc.data_path == path:
            keyed = any(abs(k.co.x - frame) < 0.01 for k in fc.keyframe_points)
            changed = kb is not None and abs(fc.evaluate(frame) - kb.value) > 1e-4
            return keyed, changed
    return False, False


def autokey_look(s):
    """Auto Keying: key the eye look after it was changed from the panel (the X / Y
    sliders are scene properties, which Blender's own auto keying doesn't cover)."""
    scene = s.id_data
    ts = scene.tool_settings
    if not ts.use_keyframe_insert_auto:
        return
    _ob, key = shape_key_data(s)
    if key is None:
        return
    f = scene.frame_current
    for name in all_look_names(s):
        if ts.auto_keying_mode == "REPLACE_KEYS" and not key_state(key, name, f)[0]:
            continue
        set_key(key, name, f, insert=True)


def is_driven(key, name):
    ad = key.animation_data
    return bool(ad and ad.drivers.find(value_path(name)))


def set_key(key, name, frame, insert):
    kb = key.key_blocks.get(name)
    if kb is None:
        return False
    if insert:
        return bool(kb.keyframe_insert("value", frame=frame, group="Face Keys"))
    try:
        return bool(kb.keyframe_delete("value", frame=frame))
    except RuntimeError:
        return False


def look_names(s, direction):
    return [n for n in (getattr(s, f"look_{direction}_1"), getattr(s, f"look_{direction}_2")) if n]


def axis_names(s, axis):
    return [n for n in (getattr(s, f"look_{axis}_1"), getattr(s, f"look_{axis}_2")) if n]


def all_look_names(s):
    out = []
    if s.look_mode == "AXIS":
        for a in LOOK_AXES:
            out += axis_names(s, a)
        return out
    for d in LOOK_DIRS:
        out += look_names(s, d)
    return out


def _dir_value(key, names):
    vals = [key.key_blocks[n].value for n in names if n in key.key_blocks]
    return max(vals) if vals else 0.0


def _set_dir(key, names, v):
    for n in names:
        kb = key.key_blocks.get(n)
        if kb is not None:
            kb.value = v


def _axis_sign(s, axis):
    return -1.0 if getattr(s, f"look_{axis}_flip") else 1.0


def _axis_value(key, names):
    for n in names:
        kb = key.key_blocks.get(n)
        if kb is not None:
            return kb.value
    return 0.0


def _set_axis(key, names, v):
    for n in names:
        kb = key.key_blocks.get(n)
        if kb is None:
            continue
        # a single key covers both directions, so it needs the negative range
        if v < kb.slider_min:
            kb.slider_min = -1.0
        kb.value = v


def get_look(s):
    _ob, key = shape_key_data(s)
    if key is None:
        return 0.0, 0.0
    if s.look_mode == "AXIS":
        x = _axis_value(key, axis_names(s, "horz")) * _axis_sign(s, "horz")
        y = _axis_value(key, axis_names(s, "vert")) * _axis_sign(s, "vert")
        return max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y))
    x = _dir_value(key, look_names(s, "right")) - _dir_value(key, look_names(s, "left"))
    y = _dir_value(key, look_names(s, "up")) - _dir_value(key, look_names(s, "down"))
    return x, y


def set_look(s, x, y):
    _ob, key = shape_key_data(s)
    if key is None:
        return
    x = max(-1.0, min(1.0, x))
    y = max(-1.0, min(1.0, y))
    if s.look_mode == "AXIS":
        _set_axis(key, axis_names(s, "horz"), x * _axis_sign(s, "horz"))
        _set_axis(key, axis_names(s, "vert"), y * _axis_sign(s, "vert"))
        return
    _set_dir(key, look_names(s, "right"), max(x, 0.0))
    _set_dir(key, look_names(s, "left"), max(-x, 0.0))
    _set_dir(key, look_names(s, "up"), max(y, 0.0))
    _set_dir(key, look_names(s, "down"), max(-y, 0.0))


def redraw(context):
    for win in context.window_manager.windows:
        for area in win.screen.areas:
            area.tag_redraw()


# --------------------------------------------------------------------------
# properties
# --------------------------------------------------------------------------

def _poll_mesh(self, ob):
    return ob.type == "MESH"


def _get_look_x(self):
    return get_look(self)[0]


def _set_look_x(self, v):
    set_look(self, v, get_look(self)[1])
    autokey_look(self)


def _get_look_y(self):
    return get_look(self)[1]


def _set_look_y(self, v):
    set_look(self, get_look(self)[0], v)
    autokey_look(self)


class FACEKEYS_PG_item(PropertyGroup):
    # "name" (built in) holds the shape key name so the list filter works
    label: StringProperty(name="Label", description="Name shown in the panel (the shape key name when empty)")
    role: EnumProperty(name="Role", items=ROLES, default="CUSTOM")


def _axis_slot(axis, i):
    what = "up (+) / down (-)" if axis == "vert" else "right (+) / left (-)"
    return StringProperty(name=f"Look {AXIS_LABEL[axis]} {i}",
                          description=f"One shape key that turns the eyes {what}"
                                      + (" (second key, e.g. the other eye)" if i == 2 else ""))


def _look_slot(direction, i):
    return StringProperty(name=f"Look {direction.title()} {i}",
                          description=f"Shape key that turns the eyes {direction}"
                                      + (" (second key, e.g. the other eye)" if i == 2 else ""))


class FACEKEYS_PG_settings(PropertyGroup):
    target: PointerProperty(name="Mesh", type=bpy.types.Object, poll=_poll_mesh,
                            description="Mesh whose shape keys are shown")
    items: CollectionProperty(type=FACEKEYS_PG_item)
    active_index: IntProperty(default=0)
    new_key: StringProperty(name="Shape Key", description="Shape key to add to the list")
    look_up_1: _look_slot("up", 1)
    look_up_2: _look_slot("up", 2)
    look_down_1: _look_slot("down", 1)
    look_down_2: _look_slot("down", 2)
    look_left_1: _look_slot("left", 1)
    look_left_2: _look_slot("left", 2)
    look_right_1: _look_slot("right", 1)
    look_right_2: _look_slot("right", 2)
    look_mode: EnumProperty(
        name="Look Keys", default="FOUR",
        items=[("FOUR", "4 Keys", "Separate keys for up, down, left and right"),
               ("AXIS", "2 Keys", "One key for up/down and one for left/right (negative value = the other way)")])
    look_vert_1: _axis_slot("vert", 1)
    look_vert_2: _axis_slot("vert", 2)
    look_horz_1: _axis_slot("horz", 1)
    look_horz_2: _axis_slot("horz", 2)
    look_vert_flip: BoolProperty(name="Flip Up/Down", description="The key turns the eyes down at positive values")
    look_horz_flip: BoolProperty(name="Flip Left/Right",
                                 description="The key turns the eyes to the character's left at positive values")
    look_x: FloatProperty(name="Look X", min=-1.0, max=1.0, get=_get_look_x, set=_set_look_x,
                          description="Left (-1) to right (+1)")
    look_y: FloatProperty(name="Look Y", min=-1.0, max=1.0, get=_get_look_y, set=_set_look_y,
                          description="Down (-1) to up (+1)")
    show_look_setup: BoolProperty(name="Look Keys", default=False)
    pad_size: IntProperty(name="Pad Size", default=90, min=40, max=300, subtype="PIXEL",
                          description="Mouse travel (pixels) for a full look to one side")


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

class FACEKEYS_UL_items(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        s = settings(context)
        _ob, key = shape_key_data(s)
        row = layout.row(align=True)
        kb = key.key_blocks.get(item.name) if key else None
        if kb is None:
            row.alert = True
            row.label(text=f"{item.label or item.name}  (missing)", icon="ERROR")
            return
        text = item.label or (ROLE_LABEL.get(item.role, "") + ": " + item.name if item.role != "CUSTOM" else item.name)
        driven = is_driven(key, item.name)
        sub = row.row(align=True)
        sub.enabled = not driven
        sub.prop(kb, "value", text=text, slider=True)
        if driven:
            row.label(text="", icon="DRIVER")
        keyed, changed = key_state(key, item.name, context.scene.frame_current)
        # state icon like Blender's property decorator: ◆ keyed on this frame, ◇ not
        # (the slider itself turns orange when its value was changed without a key)
        ic = row.row(align=True)
        ic.alert = changed
        op = ic.operator("face_keys.key_item", text="", icon="DECORATE_KEYFRAME" if keyed else "DECORATE_ANIMATE",
                         emboss=False)
        op.index = index


# --------------------------------------------------------------------------
# operators
# --------------------------------------------------------------------------

def _need_key(op, context):
    s = settings(context)
    ob, key = shape_key_data(s)
    if key is None:
        op.report({"WARNING"}, "Pick a mesh with shape keys first")
    return s, ob, key


class FACEKEYS_OT_auto_detect(Operator):
    bl_idname = "face_keys.auto_detect"
    bl_label = "Auto Detect"
    bl_description = "Find blink, eye look and mouth open shape keys by name and add them"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s, ob, key = _need_key(self, context)
        if key is None:
            return {"CANCELLED"}
        found = detect.detect_all([kb.name for kb in key.key_blocks])
        for d in LOOK_DIRS:
            ks = found["look"][d]
            setattr(s, f"look_{d}_1", ks[0] if ks else "")
            setattr(s, f"look_{d}_2", ks[1] if len(ks) > 1 else "")
        for a in LOOK_AXES:
            ks = found["look_axis"][a]
            setattr(s, f"look_{a}_1", ks[0] if ks else "")
            setattr(s, f"look_{a}_2", ks[1] if len(ks) > 1 else "")
        n_four = sum(1 for d in LOOK_DIRS if found["look"][d])
        n_axis = sum(1 for a in LOOK_AXES if found["look_axis"][a])
        if n_four or n_axis:
            s.look_mode = "AXIS" if n_axis > 0 and n_four < 2 else "FOUR"
        added = []
        for role, name in (("BLINK", found["blink"]), ("BLINK_L", found["blink_l"]),
                           ("BLINK_R", found["blink_r"]), ("MOUTH_OPEN", found["mouth_open"])):
            if not name:
                continue
            item = next((it for it in s.items if it.role == role), None)
            if item is None:
                item = next((it for it in s.items if it.name == name), None)
            if item is None:
                item = s.items.add()
            item.name, item.role = name, role
            added.append(f"{ROLE_LABEL[role]}={name}")
        look = f"look: 2 keys {n_axis}/2" if s.look_mode == "AXIS" else f"look directions: {n_four}/4"
        self.report({"INFO"}, (", ".join(added) or "no blink / mouth keys") + "; " + look)
        redraw(context)
        return {"FINISHED"}


class FACEKEYS_OT_add_item(Operator):
    bl_idname = "face_keys.add_item"
    bl_label = "Add"
    bl_description = "Add the chosen shape key to the list"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s, ob, key = _need_key(self, context)
        if key is None:
            return {"CANCELLED"}
        name = s.new_key
        if not name or name not in key.key_blocks:
            self.report({"WARNING"}, "Choose a shape key to add")
            return {"CANCELLED"}
        if any(it.name == name for it in s.items):
            self.report({"INFO"}, f"'{name}' is already in the list")
            return {"CANCELLED"}
        it = s.items.add()
        it.name = name
        s.active_index = len(s.items) - 1
        s.new_key = ""
        return {"FINISHED"}


class FACEKEYS_OT_remove_item(Operator):
    bl_idname = "face_keys.remove_item"
    bl_label = "Remove"
    bl_description = "Remove the selected entry from the list (the shape key itself is kept)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = settings(context)
        if 0 <= s.active_index < len(s.items):
            s.items.remove(s.active_index)
            s.active_index = min(s.active_index, len(s.items) - 1)
        return {"FINISHED"}


class FACEKEYS_OT_move_item(Operator):
    bl_idname = "face_keys.move_item"
    bl_label = "Move"
    bl_description = "Move the selected entry up or down"
    bl_options = {"REGISTER", "UNDO"}
    direction: EnumProperty(items=[("UP", "Up", ""), ("DOWN", "Down", "")])

    def execute(self, context):
        s = settings(context)
        i = s.active_index
        j = i - 1 if self.direction == "UP" else i + 1
        if 0 <= i < len(s.items) and 0 <= j < len(s.items):
            s.items.move(i, j)
            s.active_index = j
        return {"FINISHED"}


class FACEKEYS_OT_key_item(Operator):
    bl_idname = "face_keys.key_item"
    bl_label = "Keyframe"
    bl_description = ("Key this shape key with its current value at the current frame (updates an existing "
                      "key).  Ctrl+click: delete the key on this frame")
    bl_options = {"REGISTER", "UNDO"}
    index: IntProperty()
    delete: BoolProperty(default=False, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        self.delete = event.ctrl
        return self.execute(context)

    def execute(self, context):
        s, ob, key = _need_key(self, context)
        if key is None or not (0 <= self.index < len(s.items)):
            return {"CANCELLED"}
        name = s.items[self.index].name
        f = context.scene.frame_current
        # A click always keys the value you see.  (With Auto Keying on, moving the
        # slider has already keyed it, so a toggle would delete that key and the
        # change would be lost on the next frame change.)
        set_key(key, name, f, insert=not self.delete)
        redraw(context)
        return {"FINISHED"}


class FACEKEYS_OT_key_all(Operator):
    bl_idname = "face_keys.key_all"
    bl_label = "Key All"
    bl_options = {"REGISTER", "UNDO"}
    delete: BoolProperty(default=False)
    look_only: BoolProperty(default=False)

    @classmethod
    def description(cls, context, props):
        what = "the eye look keys" if props.look_only else "every listed shape key and the eye look keys"
        return ("Delete the keyframes of " if props.delete else "Insert keyframes on ") + what + " at the current frame"

    def execute(self, context):
        s, ob, key = _need_key(self, context)
        if key is None:
            return {"CANCELLED"}
        names = all_look_names(s)
        if not self.look_only:
            names = [it.name for it in s.items] + names
        f = context.scene.frame_current
        n = sum(1 for name in dict.fromkeys(names) if set_key(key, name, f, insert=not self.delete))
        self.report({"INFO"}, f"{'Deleted' if self.delete else 'Keyed'} {n} shape key(s) at frame {f}")
        redraw(context)
        return {"FINISHED"}


class FACEKEYS_OT_reset(Operator):
    bl_idname = "face_keys.reset"
    bl_label = "Reset"
    bl_description = "Set every listed shape key and the eye look to 0"
    bl_options = {"REGISTER", "UNDO"}
    look_only: BoolProperty(default=False)

    def execute(self, context):
        s, ob, key = _need_key(self, context)
        if key is None:
            return {"CANCELLED"}
        set_look(s, 0.0, 0.0)
        if not self.look_only:
            for it in s.items:
                kb = key.key_blocks.get(it.name)
                if kb is not None:
                    kb.value = 0.0
        redraw(context)
        return {"FINISHED"}


# ---- 2D eye look pad ------------------------------------------------------

_PAD = {"active": False, "center": (0, 0), "size": 90, "xy": (0.0, 0.0), "start": (0.0, 0.0), "sx": 1.0, "handles": []}


def _screen_x_sign(context, ob):
    """Look X (+ = character's right) per screen-right pad unit: -1 when the
    character faces the viewer (screen right is its left), +1 from behind."""
    rv3d = getattr(context.space_data, "region_3d", None)
    if rv3d is None or ob is None:
        return -1.0
    view_right = rv3d.view_matrix.to_3x3()[0]
    char_left = ob.matrix_world.to_3x3().col[0]  # +X is the character's left (faces -Y)
    return -1.0 if view_right.dot(char_left) >= 0.0 else 1.0


def _pad_draw():
    if not _PAD["active"]:
        return
    region = bpy.context.region
    if region is None:
        return
    cx = _PAD["center"][0] - region.x
    cy = _PAD["center"][1] - region.y
    r = _PAD["size"]
    if cx < -r or cy < -r or cx > region.width + r or cy > region.height + r:
        return
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    # background
    bg = [(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)]
    b = batch_for_shader(sh, "TRIS", {"pos": bg}, indices=[(0, 1, 2), (0, 2, 3)])
    sh.uniform_float("color", (0.05, 0.05, 0.06, 0.75))
    b.draw(sh)
    # frame, cross hair, start point
    lines = [bg[0], bg[1], bg[1], bg[2], bg[2], bg[3], bg[3], bg[0],
             (cx - r, cy), (cx + r, cy), (cx, cy - r), (cx, cy + r)]
    b = batch_for_shader(sh, "LINES", {"pos": lines})
    sh.uniform_float("color", (0.8, 0.8, 0.85, 0.6))
    b.draw(sh)
    sx, sy = _PAD["start"]

    def dot(px, py, rad, col):
        import math
        verts = [(px, py)] + [(px + math.cos(a / 16 * 6.2832) * rad, py + math.sin(a / 16 * 6.2832) * rad) for a in range(17)]
        bb = batch_for_shader(sh, "TRIS", {"pos": verts}, indices=[(0, i, i + 1) for i in range(1, 17)])
        sh.uniform_float("color", col)
        bb.draw(sh)

    dot(cx + sx * r, cy + sy * r, 4, (0.6, 0.6, 0.6, 0.7))
    x, y = _PAD["xy"]
    dot(cx + x * r, cy + y * r, 7, (0.3, 0.85, 1.0, 1.0))
    blf.size(0, 11)
    blf.color(0, 1, 1, 1, 0.9)
    blf.position(0, cx - r, cy + r + 6, 0)
    blf.draw(0, f"Look  X {x * _PAD['sx']:+.2f}  Y {y:+.2f}")
    blf.position(0, cx - r, cy - r - 16, 0)
    blf.draw(0, "Click / Enter: OK   RMB / Esc: cancel   Shift: fine   C: center")
    gpu.state.blend_set("NONE")


def _pad_handles_add():
    _pad_handles_remove()
    # drawn over the viewport and over the sidebar (where the button is)
    for rt in ("WINDOW", "UI"):
        _PAD["handles"].append((bpy.types.SpaceView3D.draw_handler_add(_pad_draw, (), rt, "POST_PIXEL"), rt))


def _pad_handles_remove():
    for h, rt in _PAD["handles"]:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(h, rt)
        except (ValueError, RuntimeError):
            pass
    _PAD["handles"] = []


class FACEKEYS_OT_look_pad(Operator):
    bl_idname = "face_keys.look_pad"
    bl_label = "2D Look Pad"
    bl_description = ("Move the eyes in 2D: click, move the mouse (a pad appears), click again to confirm. "
                      "Right click cancels, Shift for fine control, C centers")
    bl_options = {"REGISTER", "UNDO", "GRAB_CURSOR", "BLOCKING"}

    def invoke(self, context, event):
        s, ob, key = _need_key(self, context)
        if key is None:
            return {"CANCELLED"}
        if not all_look_names(s):
            self.report({"WARNING"}, "Set the eye look shape keys first (Auto Detect or Look Keys)")
            return {"CANCELLED"}
        self.x0, self.y0 = get_look(s)
        self.sx = _screen_x_sign(context, ob)
        px0 = self.x0 * self.sx  # pad (screen) space
        self.mouse0 = (event.mouse_x, event.mouse_y)
        self.fine_ref = None
        _PAD.update(active=True, center=(event.mouse_x - px0 * s.pad_size, event.mouse_y - self.y0 * s.pad_size),
                    size=s.pad_size, xy=(px0, self.y0), start=(px0, self.y0), sx=self.sx)
        _pad_handles_add()
        context.window_manager.modal_handler_add(self)
        redraw(context)
        return {"RUNNING_MODAL"}

    def _finish(self, context, cancel):
        s = settings(context)
        if cancel:
            set_look(s, self.x0, self.y0)
        _PAD["active"] = False
        _pad_handles_remove()
        redraw(context)
        if not cancel and context.scene.tool_settings.use_keyframe_insert_auto:
            bpy.ops.face_keys.key_all(look_only=True)
        return {"CANCELLED"} if cancel else {"FINISHED"}

    def modal(self, context, event):
        s = settings(context)
        if event.type in {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE"}:
            size = float(s.pad_size)
            mx, my = event.mouse_x, event.mouse_y
            if event.shift:
                if self.fine_ref is None:
                    self.fine_ref = (mx, my, _PAD["xy"])
                rx, ry, (fx, fy) = self.fine_ref
                x = fx + (mx - rx) / size * 0.2
                y = fy + (my - ry) / size * 0.2
            else:
                if self.fine_ref is not None:
                    # continue from where fine mode left the dot
                    fx, fy = _PAD["xy"]
                    _PAD["center"] = (mx - fx * size, my - fy * size)
                    self.fine_ref = None
                cx, cy = _PAD["center"]
                x = (mx - cx) / size
                y = (my - cy) / size
            x = max(-1.0, min(1.0, x))
            y = max(-1.0, min(1.0, y))
            set_look(s, x * self.sx, y)
            _PAD["xy"] = (x, y)
            redraw(context)
            return {"RUNNING_MODAL"}
        if event.type == "C" and event.value == "PRESS":
            set_look(s, 0.0, 0.0)
            _PAD["xy"] = (0.0, 0.0)
            _PAD["center"] = (event.mouse_x, event.mouse_y)
            self.fine_ref = None
            redraw(context)
            return {"RUNNING_MODAL"}
        if event.type in {"LEFTMOUSE", "RET", "NUMPAD_ENTER", "SPACE"} and event.value == "PRESS":
            return self._finish(context, cancel=False)
        if event.type in {"RIGHTMOUSE", "ESC"} and event.value == "PRESS":
            return self._finish(context, cancel=True)
        return {"RUNNING_MODAL"}


# --------------------------------------------------------------------------
# panel
# --------------------------------------------------------------------------

class FACEKEYS_PT_main(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Face Keys"
    bl_label = "Face Keys"

    def draw(self, context):
        layout = self.layout
        s = settings(context)
        layout.prop(s, "target", icon="MESH_DATA")
        ob, key = shape_key_data(s)
        if ob is None:
            layout.label(text="Pick the face mesh", icon="INFO")
            return
        if key is None:
            layout.label(text="This mesh has no shape keys", icon="ERROR")
            return
        row = layout.row()
        row.scale_y = 1.2
        row.operator("face_keys.auto_detect", icon="VIEWZOOM")

        # eye look
        box = layout.box()
        box.label(text="Eye Look", icon="HIDE_OFF")
        has_look = bool(all_look_names(s))
        col = box.column()
        col.enabled = has_look
        row = col.row(align=True)
        row.scale_y = 1.4
        row.operator("face_keys.look_pad", text="2D Look Pad", icon="MOUSE_MOVE")
        row.operator("face_keys.reset", text="", icon="PIVOT_CURSOR").look_only = True
        sub = col.column(align=True)
        sub.prop(s, "look_x", slider=True, text="Left  ←  X  →  Right")
        sub.prop(s, "look_y", slider=True, text="Down  ←  Y  →  Up")
        f = context.scene.frame_current
        states = [key_state(key, n, f) for n in all_look_names(s)]
        look_changed = any(c for _k, c in states)
        row = col.row(align=True)
        row.alert = look_changed
        row.operator("face_keys.key_all", text="Key Look (not keyed!)" if look_changed else "Key Look",
                     icon="KEY_HLT").look_only = True
        op = row.operator("face_keys.key_all", text="", icon="KEY_DEHLT")
        op.look_only, op.delete = True, True
        driven = [n for n in all_look_names(s) if is_driven(key, n)]
        if driven:
            w = box.column(align=True)
            w.alert = True
            w.label(text="Driven by drivers (e.g. FaceIt):", icon="DRIVER")
            w.label(text=", ".join(driven[:4]))
        if not has_look:
            box.label(text="Run Auto Detect or set Look Keys", icon="INFO")
        row = box.row()
        row.prop(s, "show_look_setup", icon="TRIA_DOWN" if s.show_look_setup else "TRIA_RIGHT", emboss=False)
        if s.show_look_setup:
            box.row().prop(s, "look_mode", expand=True)
            c = box.column(align=True)
            if s.look_mode == "AXIS":
                for a in LOOK_AXES:
                    r = c.row(align=True)
                    r.prop_search(s, f"look_{a}_1", key, "key_blocks", text=AXIS_LABEL[a])
                    r.prop_search(s, f"look_{a}_2", key, "key_blocks", text="")
                    r.prop(s, f"look_{a}_flip", text="", icon="ARROW_LEFTRIGHT")
                c.label(text="Positive = up / character's right (use flip if reversed)", icon="INFO")
            else:
                for d in LOOK_DIRS:
                    r = c.row(align=True)
                    r.prop_search(s, f"look_{d}_1", key, "key_blocks", text=d.title())
                    r.prop_search(s, f"look_{d}_2", key, "key_blocks", text="")
            box.prop(s, "pad_size")

        # listed shape keys
        box = layout.box()
        box.label(text="Shape Keys", icon="SHAPEKEY_DATA")
        row = box.row()
        row.template_list("FACEKEYS_UL_items", "", s, "items", s, "active_index", rows=6)
        col = row.column(align=True)
        col.operator("face_keys.remove_item", text="", icon="REMOVE")
        col.separator()
        col.operator("face_keys.move_item", text="", icon="TRIA_UP").direction = "UP"
        col.operator("face_keys.move_item", text="", icon="TRIA_DOWN").direction = "DOWN"
        if 0 <= s.active_index < len(s.items):
            box.prop(s.items[s.active_index], "label", text="Label")
        row = box.row(align=True)
        row.prop_search(s, "new_key", key, "key_blocks", text="")
        row.operator("face_keys.add_item", text="Add", icon="ADD")
        row = box.row(align=True)
        row.operator("face_keys.key_all", text="Key All", icon="KEY_HLT")
        row.operator("face_keys.key_all", text="Delete Keys", icon="KEY_DEHLT").delete = True
        row.operator("face_keys.reset", text="Reset", icon="LOOP_BACK")


classes = (
    FACEKEYS_PG_item, FACEKEYS_PG_settings, FACEKEYS_UL_items,
    FACEKEYS_OT_auto_detect, FACEKEYS_OT_add_item, FACEKEYS_OT_remove_item, FACEKEYS_OT_move_item,
    FACEKEYS_OT_key_item, FACEKEYS_OT_key_all, FACEKEYS_OT_reset, FACEKEYS_OT_look_pad,
    FACEKEYS_PT_main,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.face_keys = PointerProperty(type=FACEKEYS_PG_settings)


def unregister():
    _PAD["active"] = False
    _pad_handles_remove()
    del bpy.types.Scene.face_keys
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
