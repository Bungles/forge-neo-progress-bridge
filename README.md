# forge-neo-progress-bridge

A [Forge Neo](https://github.com/Haoming02/sd-webui-forge-classic) extension that exposes live ADetailer detection counts and tiled-upscaler progress over HTTP, so a client app can show real progress instead of a generic "still working" indicator. Neither signal is in Forge Neo's API today — both are patched in place from internal values and served as JSON on a new route, with no separate process or console-log scraping involved.

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
