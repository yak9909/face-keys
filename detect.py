"""Find blink, eye look and mouth-open shape keys from their names.

Works with ARKit names (eyeBlinkLeft, eyeLookUpLeft, jawOpen ...), VRChat names
(vrc.blink, vrc.v_aa), MMD morphs (まばたき, あ ...) and common custom names
(eye_close_L, eyelook_up, mouth_a ...).
"""

import re

_SEP = re.compile(r"[\s_\.\-]+")


def flat(name):
    return _SEP.sub("", name.lower())


# side of a (per-eye) key: suffix / prefix conventions
_SIDE_END = {
    "L": re.compile(r"([\s_\.\-](l|left)|left|左)$", re.I),
    "R": re.compile(r"([\s_\.\-](r|right)|right|右)$", re.I),
}
_SIDE_START = {
    "L": re.compile(r"^(l[\s_\.\-]|left|左)", re.I),
    "R": re.compile(r"^(r[\s_\.\-]|right|右)", re.I),
}


def side_of(name):
    for s in ("L", "R"):
        if _SIDE_END[s].search(name) or _SIDE_START[s].search(name):
            return s
    return None


def strip_side(name):
    n = re.sub(r"([\s_\.\-](l|r|left|right)|left|right|左|右)$", "", name, flags=re.I)
    n = re.sub(r"^(l[\s_\.\-]|r[\s_\.\-]|left|right|左|右)", "", n, flags=re.I)
    return n


def _digit_rank(name):
    m = re.search(r"(\d+)", name)
    return int(m.group(1)) if m else -1


# --------------------------------------------------------------------------
# blink
# --------------------------------------------------------------------------

_BLINK_WORDS = ("blink", "eyeclose", "closeeye", "eyesclose", "closedeye", "まばたき", "瞬き", "目閉じ", "目を閉じ")
_BLINK_EXCLUDE = ("half", "smile", "笑", "wink", "ウィンク", "ｳｨﾝｸ", "sad", "angry", "happy")
_BLINK_BEST = ("blink", "eyeblink", "eyesblink", "まばたき", "瞬き", "eyeclose", "eyesclosed")


def _blink_candidates(names):
    out = []
    for n in names:
        f = flat(n)
        if any(w in f for w in _BLINK_WORDS) and not any(x in f for x in _BLINK_EXCLUDE):
            out.append(n)
    return out


def detect_blink(names):
    """(both, left, right) key names or None each."""
    cands = _blink_candidates(names)
    left = [c for c in cands if side_of(c) == "L"]
    right = [c for c in cands if side_of(c) == "R"]
    both = [c for c in cands if side_of(c) is None]
    bases_lr = {strip_side(c) for c in left} & {strip_side(c) for c in right}

    def rank(n):
        f = flat(n)
        return (0 if n in bases_lr else 1, 0 if f in _BLINK_BEST else 1, _digit_rank(n), len(n))

    b = min(both, key=rank) if both else None
    base = b if b in bases_lr else None

    def pick(lst):
        if not lst:
            return None
        if base:
            same = [x for x in lst if strip_side(x) == base]
            if same:
                return same[0]
        return min(lst, key=lambda n: (0 if flat(strip_side(n)) in _BLINK_BEST else 1, _digit_rank(n), len(n)))

    return b, pick(left), pick(right)


# --------------------------------------------------------------------------
# eye look
# --------------------------------------------------------------------------

_ARKIT_LOOK = re.compile(r"eyelook(up|down|in|out)(left|right)$")
_LOOK_WORDS = ("look", "視線", "目線")
_DIR_WORDS = {
    "up": ("up", "上"),
    "down": ("down", "下"),
    "left": ("left", "左"),
    "right": ("right", "右"),
}


def detect_look(names):
    """{"up": [k1, k2], "down": [...], "left": [...], "right": [...]} (up to two keys each).

    "left" / "right" are the character's left / right.  ARKit per-eye keys are
    combined: looking to the character's left = left eye out + right eye in."""
    out = {d: [] for d in _DIR_WORDS}
    for n in names:
        m = _ARKIT_LOOK.match(flat(n))
        if not m:
            continue
        mode, eye = m.group(1), m.group(2)
        if mode in ("up", "down"):
            out[mode].append(n)
        elif (mode == "out") == (eye == "left"):
            out["left"].append(n)
        else:
            out["right"].append(n)
    if any(out.values()):
        return {d: sorted(v)[:2] for d, v in out.items()}
    for n in names:
        f = flat(n)
        if not any(w in f for w in _LOOK_WORDS) or "lid" in f:
            continue
        for d, words in _DIR_WORDS.items():
            # the direction word right after "look" (eyelook_up, look_down_L, 視線上 ...)
            if any(re.search(r"(look|視線|目線)" + re.escape(w), f) for w in words):
                out[d].append(n)
                break
    for d in out:
        out[d] = sorted(out[d], key=lambda n: (side_of(n) or "", len(n)))[:2]
    return out


_AXIS_WORDS = {
    "vert": ("updown", "downup", "上下", "vertical"),
    "horz": ("leftright", "rightleft", "左右", "horizontal"),
}
_AXIS_EXCLUDE = ("mouth", "口", "brow", "眉", "jaw", "head", "頭", "neck", "首")


def detect_look_axis(names):
    """{"vert": [...], "horz": [...]}: single keys that move the eyes along a whole
    axis (EyeUpDown, 目_左右 ...), up to two each (e.g. one per eye)."""
    out = {a: [] for a in _AXIS_WORDS}
    for n in names:
        f = flat(n)
        if any(x in f for x in _AXIS_EXCLUDE):
            continue
        for a, words in _AXIS_WORDS.items():
            if any(w in f for w in words):
                out[a].append(n)
                break
    for a in out:
        # prefer keys that mention the eyes / look
        out[a] = sorted(out[a], key=lambda n: (0 if any(w in flat(n) for w in ("eye", "look", "目", "視線")) else 1,
                                               side_of(n) or "", len(n)))[:2]
    return out


# --------------------------------------------------------------------------
# mouth open
# --------------------------------------------------------------------------

_MOUTH_EXACT = ("jawopen", "mouthopen", "openmouth", "moutha", "口開け", "あ", "vrcvaa", "aa", "mouthaa", "a")
_MOUTH_CONTAINS = ("jawopen", "mouthopen", "口開")


def detect_mouth_open(names):
    fl = {flat(n): n for n in names}
    for e in _MOUTH_EXACT:
        if e in fl:
            return fl[e]
    for n in names:
        if any(w in flat(n) for w in _MOUTH_CONTAINS):
            return n
    return None


def detect_all(names):
    names = [n for n in names if n and n.lower() != "basis"]
    both, left, right = detect_blink(names)
    return dict(blink=both, blink_l=left, blink_r=right, look=detect_look(names), look_axis=detect_look_axis(names),
                mouth_open=detect_mouth_open(names))
