# forge-neo-progress-bridge

A [Forge Neo](https://github.com/Haoming02/sd-webui-forge-classic) extension that exposes live ADetailer detection counts and tiled-upscaler tile progress over HTTP, so a client app can show real progress instead of a generic "still working" indicator.

Forge Neo doesn't expose either signal through its API; both exist only as internal Python values inside ADetailer's own code and inside a `tqdm` progress bar buried in `modules/upscaler_utils.py`. This extension hooks both in place (via targeted monkey-patches, not console-log scraping) and serves the results as JSON from a new route on Forge's own webserver. No separate process, no polling Forge's console output, no dependency on any other tool.
