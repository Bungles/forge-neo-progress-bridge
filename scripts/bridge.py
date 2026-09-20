# forge-neo-progress-bridge
#
# Forge Neo extension exposing ADetailer face/hand detection counts and
# tiled-upscaler tile progress via GET /forge-neo-progress-bridge/status,
# so Resolver can show real progress instead of generic indicators.
#
# Install: drop this file at
#   extensions/forge-neo-progress-bridge/scripts/bridge.py
#
# All logging here goes through plain print(), not the `logging` module -
# Forge Neo's logging setup was found to silently swallow this extension's
# logger.info()/logger.warning() calls during early testing (root cause
# never pinned down), which briefly looked like the patches themselves were
# failing. print() reliably reaches Forge's console, so it's used
# throughout, including for real (non-diagnostic) status messages. Don't
# "clean this up" back to logging without re-confirming that's fixed.
#
# Full design history, done-test evidence, and the rationale behind each
# patch (unit_index, batch_index, the postprocess_image reset, why
# `errors` isn't a louder failure mode, etc.) lives in workflow.md in this
# project - that's the authoritative changelog. Keep it in sync rather than
# re-narrating history here: duplicated rationale in this header previously
# drifted out of date (claimed unit_index/batch_index were unverified after
# both had already been live-verified in workflow.md).

import sys
import threading
import traceback

try:
    from modules import script_callbacks
except Exception:
    print("[forge-neo-progress-bridge] FAILED to import modules.script_callbacks:")
    print(traceback.format_exc())
    raise

_state_lock = threading.Lock()
_state = {
    "adetailer": {
        "active": False,
        "count": None,
        "detector": None,
        "unit_index": None,
        "batch_index": None,
    },
    "upscale": {"active": False, "current": None, "total": None, "desc": None},
}

# Keyed issues rather than an append-only log, so a problem that gets fixed
# (e.g. ADetailer patch succeeds on a later retry) disappears from "errors"
# instead of leaving a stale message behind forever.
_issues_lock = threading.Lock()
_issues = {
    "adetailer": None,
    "adetailer_reset": None,
    "adetailer_unit_index": None,
    "adetailer_batch_index": None,
    "adetailer_extras_reset": None,
    "tqdm": None,
}

# Set by the _postprocess_image_inner patch just before it calls into the
# (separately patched) predict functions, and read by them at the moment
# they fire - passes `n` (which unit, in order) across a call boundary the
# predict functions don't otherwise have access to. Guarded by _state_lock
# since it's read/written from the same generation-thread context as
# _state itself, not because it's expected to be contended.
_current_unit_index = None


def _set_current_unit_index(n):
    global _current_unit_index
    with _state_lock:
        _current_unit_index = n


def _get_current_unit_index():
    with _state_lock:
        return _current_unit_index


# batch_index tracking. _batch_index_counter is the free-running "next value
# to hand out" counter, advanced once per image (from the existing
# postprocess_image reset hook - see _ensure_adetailer_reset_patched).
# _current_batch_index is the value captured for whichever image is
# currently being processed, read by _wrap_predict the same way
# _current_unit_index is. The counter itself is only ever reset to 0 by the
# separate process_batch patch (_ensure_adetailer_batch_index_patched), which
# sets _batch_index_patch_ok on success - if that patch never attaches, the
# counter still ticks upward on every image but _get_current_batch_index()
# reports null instead of the (meaningless, never-reset) number, since a
# wrong batch_index would actively mislead Resolver rather than just being
# absent.
_batch_index_counter = 0
_current_batch_index = None
_batch_index_patch_ok = False


def _reset_batch_index_counter():
    global _batch_index_counter
    with _state_lock:
        _batch_index_counter = 0


def _consume_batch_index():
    """Called once per image, from the postprocess_image reset hook -
    captures this image's batch_index and advances the counter for the next
    image in the same batch."""
    global _batch_index_counter, _current_batch_index
    with _state_lock:
        _current_batch_index = _batch_index_counter
        _batch_index_counter += 1


def _get_current_batch_index():
    with _state_lock:
        if not _batch_index_patch_ok:
            return None
        return _current_batch_index


def _set_issue(key, msg):
    with _issues_lock:
        was_set = _issues.get(key) is not None
        _issues[key] = msg
    if not was_set:
        print(f"[forge-neo-progress-bridge] WARNING: {msg}")


def _clear_issue(key, ok_msg=None):
    with _issues_lock:
        was_set = _issues.get(key) is not None
        _issues[key] = None
    if was_set and ok_msg:
        print(f"[forge-neo-progress-bridge] {ok_msg}")


def _set_adetailer(count, detector, unit_index, batch_index):
    with _state_lock:
        _state["adetailer"] = {
            "active": True,
            "count": count,
            "detector": detector,
            "unit_index": unit_index,
            "batch_index": batch_index,
        }


def _clear_adetailer():
    with _state_lock:
        _state["adetailer"] = {
            "active": False,
            "count": None,
            "detector": None,
            "unit_index": None,
            "batch_index": None,
        }


def _set_upscale(current, total, desc):
    with _state_lock:
        _state["upscale"] = {"active": True, "current": current, "total": total, "desc": desc}


def _clear_upscale():
    with _state_lock:
        _state["upscale"] = {"active": False, "current": None, "total": None, "desc": None}


# --------------------------------------------------------------------------
# Stage 1 — ADetailer face/hand counts
#
# adetailer.py does `from lib_adetailer import ultralytics_predict,
# mediapipe_predict` at import time, so patching the originals in
# lib_adetailer would NOT affect adetailer.py's already-bound copies of
# those names. We have to patch the names inside adetailer.py's own module
# object.
#
# Finding that module object is the real gotcha. Forge/A1111-family
# extension loading (modules/scripts.py -> modules/script_loading.py) loads
# each extension script with importlib.util.spec_from_file_location() +
# module_from_spec() + exec_module() called directly - and that path does
# NOT insert the module into sys.modules (that only happens via the normal
# import machinery / importlib.import_module()). Confirmed against a real
# Forge Neo run: a sys.modules scan reliably came back empty even with
# ADetailer-Neo installed and enabled.
#
# The module object is still reachable, though: Forge records it on
# modules.scripts.scripts_data when it registers the Script subclasses each
# extension file defines. We walk that registry and, for each entry, look at
# whatever module object it's carrying - matching by the module's __file__
# rather than by any specific attribute name on the registry entry, since
# that shape isn't guaranteed to be identical across Forge/A1111 forks.
# sys.modules is still checked too, cheaply, as a harmless first pass in
# case a given fork's loader does register it there.
# --------------------------------------------------------------------------

def _is_adetailer_module(mod):
    f = getattr(mod, "__file__", None)
    if not f:
        return False
    norm = f.replace("\\", "/")
    return norm.endswith("/scripts/adetailer.py") and "adetailer" in norm.lower()


def _iter_candidate_modules():
    for mod in list(sys.modules.values()):
        yield mod

    try:
        from modules import scripts as forge_scripts
    except Exception:
        return

    for entry in list(getattr(forge_scripts, "scripts_data", [])):
        mod = getattr(entry, "module", None)
        if mod is not None:
            yield mod


def _find_adetailer_module():
    for mod in _iter_candidate_modules():
        if _is_adetailer_module(mod):
            return mod
    return None


def _find_adetailer_script_class(mod):
    """The Script subclass ADetailer-Neo registers, found by looking inside
    the already-located adetailer.py module rather than by name - avoids
    depending on what ADetailer-Neo happens to call the class, same spirit
    as matching the module by __file__ instead of by sys.modules name."""
    try:
        from modules import scripts as forge_scripts
    except Exception:
        return None

    base = getattr(forge_scripts, "Script", None)
    if base is None:
        return None

    for value in list(vars(mod).values()):
        if isinstance(value, type) and value is not base and issubclass(value, base):
            return value
    return None


def _wrap_predict(orig_fn, detector_label):
    def wrapped(*args, **kwargs):
        pred = orig_fn(*args, **kwargs)
        try:
            _set_adetailer(
                len(pred.bboxes),
                detector_label,
                _get_current_unit_index(),
                _get_current_batch_index(),
            )
        except Exception:
            print(
                f"[forge-neo-progress-bridge] WARNING: couldn't read .bboxes off "
                f"{detector_label}'s return value:"
            )
            print(traceback.format_exc())
        return pred

    return wrapped


_adetailer_fully_patched = False


def _ensure_adetailer_patched():
    """Idempotent. Once all four sub-patches (predict-hooking,
    state-resetting, unit_index, batch_index) have succeeded, short-circuits
    on _adetailer_fully_patched instead of re-scanning sys.modules /
    scripts_data and re-walking the Script class's attributes on every
    single /status poll - that full discovery only runs while something is
    still unpatched, so a startup-ordering hiccup keeps self-healing without
    a Forge restart, but the steady state (the vast majority of polls) is a
    single boolean check. count/active are the only two guaranteed to work
    if everything past predict-hooking fails to attach."""
    global _adetailer_fully_patched
    if _adetailer_fully_patched:
        return True

    mod = _find_adetailer_module()
    if mod is None:
        msg = (
            "Could not find ADetailer-Neo's adetailer.py module - face/hand "
            "counts won't be reported. Is ADetailer-Neo installed and "
            "enabled?"
        )
        _set_issue("adetailer", msg)
        _set_issue("adetailer_reset", msg)
        return False

    predict_ok = getattr(mod, "_forge_neo_progress_bridge_patched", False)
    if predict_ok:
        _clear_issue("adetailer")
    else:
        orig_ultralytics = getattr(mod, "ultralytics_predict", None)
        orig_mediapipe = getattr(mod, "mediapipe_predict", None)
        if orig_ultralytics is None or orig_mediapipe is None:
            _set_issue(
                "adetailer",
                "adetailer.py no longer has ultralytics_predict/mediapipe_predict "
                "at module scope - ADetailer-Neo's internals changed, this patch "
                "needs updating.",
            )
        else:
            mod.ultralytics_predict = _wrap_predict(orig_ultralytics, "ultralytics")
            mod.mediapipe_predict = _wrap_predict(orig_mediapipe, "mediapipe")
            mod._forge_neo_progress_bridge_patched = True
            _clear_issue("adetailer", f"ADetailer hook installed on {mod.__file__}")
            predict_ok = True

    # Found once here and passed down, rather than each sub-patch below
    # re-running its own _find_adetailer_script_class() scan.
    script_class = _find_adetailer_script_class(mod)

    reset_ok = _ensure_adetailer_reset_patched(script_class)
    unit_index_ok = _ensure_adetailer_unit_index_patched(script_class)
    batch_index_ok = _ensure_adetailer_batch_index_patched(script_class)

    _adetailer_fully_patched = predict_ok and reset_ok and unit_index_ok and batch_index_ok
    return _adetailer_fully_patched


def _ensure_adetailer_reset_patched(script_class):
    """Clears adetailer state at the start of every image's
    postprocess_image() call (fires once per image regardless of unit
    count) so a stale value from a previous image/run doesn't linger, and
    advances the batch_index counter at the same boundary - no separate
    per-image hook needed for that the way unit_index needed
    _postprocess_image_inner. See workflow.md for full rationale and
    done-test evidence."""
    if script_class is None:
        _set_issue(
            "adetailer_reset",
            "Could not find ADetailer-Neo's Script class - stale "
            "adetailer.active values from a previous run may persist into "
            "generations with no ADetailer units configured.",
        )
        return False

    if getattr(script_class, "_forge_neo_progress_bridge_reset_patched", False):
        _clear_issue("adetailer_reset")
        return True

    orig_postprocess_image = getattr(script_class, "postprocess_image", None)
    if orig_postprocess_image is None:
        _set_issue(
            "adetailer_reset",
            f"{script_class.__name__} has no postprocess_image method - "
            "ADetailer-Neo's internals changed, this patch needs updating.",
        )
        return False

    def patched_postprocess_image(self, *args, **kwargs):
        _clear_adetailer()
        _consume_batch_index()
        return orig_postprocess_image(self, *args, **kwargs)

    script_class.postprocess_image = patched_postprocess_image
    script_class._forge_neo_progress_bridge_reset_patched = True
    _clear_issue(
        "adetailer_reset",
        f"ADetailer reset hook installed on {script_class.__name__}.postprocess_image",
    )
    return True


def _ensure_adetailer_unit_index_patched(script_class):
    """Captures `n` (which configured ADetailer unit) from
    _postprocess_image_inner so unit_index can be reported. Independent of
    the other sub-patches - if this fails, count/active still work and
    unit_index just stays null. See workflow.md for full rationale and
    done-test evidence."""
    if script_class is None:
        _set_issue(
            "adetailer_unit_index",
            "Could not find ADetailer-Neo's Script class - adetailer.unit_index "
            "won't be reported (count/active are unaffected).",
        )
        return False

    if getattr(script_class, "_forge_neo_progress_bridge_unit_index_patched", False):
        _clear_issue("adetailer_unit_index")
        return True

    orig_postprocess_image_inner = getattr(script_class, "_postprocess_image_inner", None)
    if orig_postprocess_image_inner is None:
        _set_issue(
            "adetailer_unit_index",
            f"{script_class.__name__} has no _postprocess_image_inner method - "
            "ADetailer-Neo's internals changed, this patch needs updating "
            "(count/active are unaffected).",
        )
        return False

    def patched_postprocess_image_inner(self, *args, **kwargs):
        _set_current_unit_index(kwargs.get("n", 0))
        return orig_postprocess_image_inner(self, *args, **kwargs)

    script_class._postprocess_image_inner = patched_postprocess_image_inner
    script_class._forge_neo_progress_bridge_unit_index_patched = True
    _clear_issue(
        "adetailer_unit_index",
        f"ADetailer unit-index hook installed on {script_class.__name__}._postprocess_image_inner",
    )
    return True


def _ensure_adetailer_batch_index_patched(script_class):
    """Patches Script.process_batch (fires once per batch-size iteration) to
    reset the batch_index counter to 0; the per-image advance piggybacks on
    the existing reset hook in _ensure_adetailer_reset_patched(). If this
    fails to attach, the counter never resets, so _get_current_batch_index()
    reports null rather than a free-running, misleading number - see
    workflow.md for full rationale and done-test evidence."""
    global _batch_index_patch_ok

    if script_class is None:
        _set_issue(
            "adetailer_batch_index",
            "Could not find ADetailer-Neo's Script class - adetailer.batch_index "
            "won't be reported (count/active/unit_index are unaffected).",
        )
        with _state_lock:
            _batch_index_patch_ok = False
        return False

    if getattr(script_class, "_forge_neo_progress_bridge_batch_index_patched", False):
        _clear_issue("adetailer_batch_index")
        with _state_lock:
            _batch_index_patch_ok = True
        return True

    orig_process_batch = getattr(script_class, "process_batch", None)
    if orig_process_batch is None:
        _set_issue(
            "adetailer_batch_index",
            f"{script_class.__name__} has no process_batch method - Forge Neo's "
            "Script base class API doesn't match what this patch expects "
            "(count/active/unit_index are unaffected).",
        )
        with _state_lock:
            _batch_index_patch_ok = False
        return False

    def patched_process_batch(self, *args, **kwargs):
        _reset_batch_index_counter()
        return orig_process_batch(self, *args, **kwargs)

    script_class.process_batch = patched_process_batch
    script_class._forge_neo_progress_bridge_batch_index_patched = True
    _clear_issue(
        "adetailer_batch_index",
        f"ADetailer batch-index hook installed on {script_class.__name__}.process_batch",
    )
    with _state_lock:
        _batch_index_patch_ok = True
    return True


# --------------------------------------------------------------------------
# Stage 2 — tiled upscaler progress
#
# upscale_with_model_cpu/gpu (modules/upscaler_utils.py) drive a
# tqdm.tqdm(total=grid.tile_count, desc=desc, ...) bar, one p.update(1) per
# completed tile, where desc contains "CPU Composite" or "GPU Composite".
# Re-implementing that loop ourselves would drift out of sync with Forge's
# real one, so instead we patch tqdm.tqdm itself and filter by desc - stable
# across Forge updates, and covers both CPU and GPU Composite for free.
#
# update() fires for every progress bar Forge creates (training steps, model
# loading, etc.), not just tile ones, so it has to stay cheap: one dict
# lookup via a bool instance attribute, no string work on the hot path.
#
# Unlike the ADetailer patch, tqdm is a normal pip package imported the
# normal way, so it's always in sys.modules once `import tqdm` below runs -
# no equivalent gotcha here. Confirmed live: GPU Composite runs show up
# (desc "tiled upscale (GPU Composite)"), current/total track the real tile
# grid, and the state clears back to inactive once the bar closes.
# --------------------------------------------------------------------------

def _ensure_tqdm_patched():
    try:
        import tqdm as tqdm_module
    except Exception:
        _set_issue("tqdm", "Could not import tqdm - upscale progress won't be reported.")
        return False

    tqdm_cls = tqdm_module.tqdm
    if getattr(tqdm_cls, "_forge_neo_progress_bridge_patched", False):
        _clear_issue("tqdm")
        return True

    orig_init = tqdm_cls.__init__
    orig_update = tqdm_cls.update
    orig_close = tqdm_cls.close

    def patched_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        desc = kwargs.get("desc", getattr(self, "desc", None))
        track = bool(desc) and "Composite" in str(desc)
        self._forge_neo_progress_bridge_track = track
        if track:
            _set_upscale(current=getattr(self, "n", 0), total=getattr(self, "total", None), desc=str(desc))

    def patched_update(self, n=1):
        result = orig_update(self, n)
        if getattr(self, "_forge_neo_progress_bridge_track", False):
            _set_upscale(current=self.n, total=self.total, desc=getattr(self, "desc", None))
        return result

    def patched_close(self):
        was_tracked = getattr(self, "_forge_neo_progress_bridge_track", False)
        result = orig_close(self)
        if was_tracked:
            _clear_upscale()
        return result

    tqdm_cls.__init__ = patched_init
    tqdm_cls.update = patched_update
    tqdm_cls.close = patched_close
    tqdm_cls._forge_neo_progress_bridge_patched = True
    _clear_issue("tqdm", "tqdm hook installed")
    return True


# --------------------------------------------------------------------------
# Extras-tab stale-adetailer fix
#
# adetailer state only resets via ADetailer-Neo's own Script.postprocess_image
# hook (see _ensure_adetailer_reset_patched), which is part of the
# txt2img/img2img Script pipeline. The Extras tab doesn't run that pipeline,
# so a generation with an ADetailer unit followed by an Extras-tab upscale of
# that same image left adetailer.active/count/unit_index/batch_index frozen
# at their old values for the entire upscale and after - CONFIRMED live: a
# real GPU Composite Extras run showed upscale tracking 0->25 correctly while
# adetailer sat stuck at active:true/count:1/unit_index:0/batch_index:0 the
# whole time.
#
# Fix: patch modules.postprocessing.run_postprocessing - the Extras tab's
# entry point in the standard A1111/Forge module layout - to clear adetailer
# state right at the start of every Extras job. Independent of the other
# adetailer sub-patches (doesn't even require ADetailer-Neo to be installed),
# so it's tried unconditionally rather than gated behind finding ADetailer's
# module.
#
# LIVE-VERIFIED: run_postprocessing is the right entry point on Forge Neo -
# repeating the exact scenario above showed adetailer flip to active:false
# right as the Extras job starts (before upscale even begins tracking tiles)
# and stay correctly cleared through the whole run. See workflow.md.
# --------------------------------------------------------------------------

def _ensure_extras_reset_patched():
    try:
        from modules import postprocessing as forge_postprocessing
    except Exception:
        _set_issue(
            "adetailer_extras_reset",
            "Could not import modules.postprocessing - adetailer state may go "
            "stale during an Extras-tab job that follows a generation with an "
            "ADetailer unit active.",
        )
        return False

    orig_run_postprocessing = getattr(forge_postprocessing, "run_postprocessing", None)
    if orig_run_postprocessing is None:
        _set_issue(
            "adetailer_extras_reset",
            "modules.postprocessing has no run_postprocessing function - "
            "Forge Neo's Extras-tab entry point doesn't match what this patch "
            "expects, needs updating.",
        )
        return False

    if getattr(orig_run_postprocessing, "_forge_neo_progress_bridge_patched", False):
        _clear_issue("adetailer_extras_reset")
        return True

    def patched_run_postprocessing(*args, **kwargs):
        _clear_adetailer()
        return orig_run_postprocessing(*args, **kwargs)

    patched_run_postprocessing._forge_neo_progress_bridge_patched = True
    forge_postprocessing.run_postprocessing = patched_run_postprocessing
    _clear_issue(
        "adetailer_extras_reset",
        "Extras-tab adetailer reset hook installed on "
        "modules.postprocessing.run_postprocessing",
    )
    return True


# --------------------------------------------------------------------------
# Stage 3 — the route
# --------------------------------------------------------------------------

def _status_payload():
    # Retries any not-yet-successful patch on every poll until it attaches
    # (see _ensure_adetailer_patched's fast-path once fully patched).
    _ensure_adetailer_patched()
    _ensure_tqdm_patched()
    _ensure_extras_reset_patched()

    with _issues_lock:
        errors = [msg for msg in _issues.values() if msg]

    with _state_lock:
        return {
            "adetailer": dict(_state["adetailer"]),
            "upscale": dict(_state["upscale"]),
            "errors": errors,
        }


def bridge_api(_demo, app):
    try:
        _ensure_adetailer_patched()
        _ensure_tqdm_patched()
        _ensure_extras_reset_patched()

        @app.get("/forge-neo-progress-bridge/status")
        async def _status():
            return _status_payload()

        print("[forge-neo-progress-bridge] route registered: /forge-neo-progress-bridge/status")
    except Exception:
        print("[forge-neo-progress-bridge] bridge_api() raised an exception:")
        print(traceback.format_exc())
        raise


try:
    script_callbacks.on_app_started(bridge_api)
except Exception:
    print("[forge-neo-progress-bridge] FAILED to register on_app_started callback:")
    print(traceback.format_exc())
    raise
