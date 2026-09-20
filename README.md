# forge-neo-progress-bridge

A [Forge Neo](https://github.com/Haoming02/sd-webui-forge-classic) extension that exposes live ADetailer detection counts and tiled-upscaler progress over HTTP, so a client app can show real progress instead of a generic "still working" indicator. Neither signal is in Forge Neo's API today — both are patched in place from internal values and served as JSON on a new route, with no separate process or console-log scraping involved.

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

- **`adetailer`** — most recent detection result. `active: false` means nothing to report (not an error); `count`/`detector` are the detection and its backend; `unit_index` is which configured ADetailer unit it belongs to (resets every image); `batch_index` is which image slot within a true batch-size (>1) job it belongs to (advances every image, resets once per batch-size job, independent of `unit_index`). Both index fields can be `null` while `active` is `true` if their specific internal patch hasn't attached. Resets on every new image and on switching to an unrelated job (e.g. Extras tab), so it never goes stale.
- **`upscale`** — progress of the current tiled upscale (CPU or GPU Composite), covering both a normal generation's postprocessing upscaler and the standalone Extras-tab upscaler. `current`/`total` are tiles done/total; `desc` is a human-readable label.
- **`errors`** — normally empty; non-empty means one internal patch failed to attach (e.g. after a Forge/ADetailer update), so that one signal won't have real data, but nothing else breaks.

Treat `active: false`, and the endpoint being unreachable at all, the same way: no data right now, fall back to your own generic progress indicator.

## Install

```
cd <your Forge Neo install>/extensions
git clone https://github.com/<your-username>/forge-neo-progress-bridge.git
```

Restart Forge Neo. No configuration needed — if ADetailer-Neo isn't installed, `adetailer` just always reports `active: false`.

## Requirements

- [Forge Neo](https://github.com/Haoming02/sd-webui-forge-classic) (the `neo` branch of sd-webui-forge-classic)
- [ADetailer-Neo](https://github.com/Haoming02/ADetailer-Neo), for the `adetailer` signal — `upscale` works without it

## How it works

ADetailer's detection functions and a few of its Script hooks are monkey-patched in place to capture counts, unit index, and batch index, and to reset stale state between images and jobs. `tqdm.tqdm` itself is patched and filtered to bars whose description contains "Composite" to track tile progress, covering both CPU/GPU Composite paths regardless of which Forge code calls into them. Every patch is independent and retried on every poll until it attaches, so a startup-ordering hiccup self-heals without a restart.
