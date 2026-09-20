# forge-neo-progress-bridge

A [Forge Neo](https://github.com/Haoming02/sd-webui-forge-classic) extension that exposes live ADetailer detection counts and tiled-upscaler tile progress over HTTP, so a client app can show real progress instead of a generic "still working" indicator.

Forge Neo doesn't expose either signal through its API today — both exist only as internal Python values inside ADetailer's own code and inside a `tqdm` progress bar buried in `modules/upscaler_utils.py`. This extension hooks both in place (via targeted monkey-patches, not console-log scraping) and serves the results as JSON from a new route on Forge's own webserver. No separate process, no polling Forge's console output, no dependency on any other tool.

## What it reports

```
GET /forge-neo-progress-bridge/status
```

```json
{
  "adetailer": {
    "active": true,
    "count": 1,
    "detector": "ultralytics",
    "unit_index": 0,
    "batch_index": 0
  },
  "upscale": {
    "active": false,
    "current": null,
    "total": null,
    "desc": null
  },
  "errors": []
}
```

- **`adetailer`** — the most recent ADetailer face/hand detection result.
  - `active`: `false` means nothing to report right now (no ADetailer unit configured for the current image). `true` means the rest of the fields are real, including a genuine `count: 0`.
  - `count`: number of detections from the most recent ADetailer unit that ran.
  - `detector`: which backend produced it, `"ultralytics"` or `"mediapipe"`.
  - `unit_index`: which configured ADetailer unit (0-based, in configured order — faces tab, hands tab, etc.) the current `count` belongs to. Resets every image.
  - `batch_index`: which image slot within a true batch-size (>1) job — several images sharing one base sampling pass — the current detection belongs to. Advances every image, resets once per batch-size job. Independent of `unit_index`: for a 2-unit, batch-size-3 job you'd see `(batch_index 0, unit_index 0)`, `(batch_index 0, unit_index 1)`, `(batch_index 1, unit_index 0)`, `(batch_index 1, unit_index 1)`, `(batch_index 2, unit_index 0)`, `(batch_index 2, unit_index 1)`. Can be `null` even while `active` is `true` — a deliberate fail-safe if the underlying patch hasn't attached, rather than reporting a free-running, meaningless number.
  - Resets to inactive at the start of every generated image, and also when switching to an unrelated job (e.g. the Extras tab), so a stale value never lingers.
- **`upscale`** — progress of the current tiled upscale (CPU or GPU Composite), covering both the postprocessing-stage upscaler during a normal generation and the standalone Extras-tab upscaler.
  - `active`: `false` means no tiled upscale running right now.
  - `current` / `total`: tiles completed / total tiles.
  - `desc`: a human-readable label, e.g. `"tiled upscale (GPU Composite)"`.
- **`errors`**: array of strings, normally empty. Non-empty means one of this extension's internal patches failed to attach to something inside Forge Neo or ADetailer (e.g. after an update changed an internal API) — the affected signal(s) won't have real data, but nothing else breaks.

`active: false` (or the endpoint being unreachable at all) should always be treated as "no data available right now," not as an error — a client polling this should fall back to its own existing generic progress indicator in either case.

## Install

Clone this repo into your Forge Neo `extensions` folder:

```
cd <your Forge Neo install>/extensions
git clone https://github.com/<your-username>/forge-neo-progress-bridge.git
```

so it lands at `extensions/forge-neo-progress-bridge/scripts/bridge.py`. Restart Forge Neo — it auto-loads every `.py` directly under an extension's `scripts/` folder.

No configuration needed. If ADetailer-Neo isn't installed, `adetailer` just always reports `active: false` (with a message in `errors`); if a tiled upscale never runs, `upscale` stays `active: false`. Nothing here depends on any other extension.

## Requirements

- [Forge Neo](https://github.com/Haoming02/sd-webui-forge-classic) (the `neo` branch of sd-webui-forge-classic)
- [ADetailer-Neo](https://github.com/Haoming02/adetailer-forge-classic) installed and enabled, for the `adetailer` signal specifically — `upscale` works without it

## How it works, briefly

- **ADetailer counts**: `adetailer.py`'s two detection functions (`ultralytics_predict`, `mediapipe_predict`) are wrapped in place, inside ADetailer's own module object (found by matching `__file__` against Forge's `scripts_data` registry, since Forge's extension loader doesn't register extension modules in `sys.modules`). A second patch on `Script.postprocess_image` clears stale state at the start of every image. A third on `_postprocess_image_inner` captures which configured unit is currently running. A fourth on `Script.process_batch` tracks true batch-size image slots. A fifth on `modules.postprocessing.run_postprocessing` clears state when an unrelated Extras-tab job starts.
- **Upscale progress**: `tqdm.tqdm` itself is patched (`__init__`/`update`/`close`), filtered to bars whose description contains `"Composite"` — this covers both CPU and GPU Composite tiled upscaling, wherever in Forge Neo they're triggered from, without depending on which specific function calls into `tqdm`.
- Every patch is independent and retried on every `/status` poll until it succeeds, so a one-off startup-ordering issue self-heals without a Forge restart, and a failure in one signal never breaks another.

## License

MIT (or update to whatever you prefer before publishing).
