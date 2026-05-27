# Icons

Tauri requires the following files in this directory before `tauri build`:

* `32x32.png`
* `128x128.png`
* `128x128@2x.png`
* `icon.icns`  (macOS, optional on Windows-only builds)
* `icon.ico`   (required on Windows)

Generate them all from a single 1024×1024 source PNG:

```powershell
npx @tauri-apps/cli icon path\to\source.png
```

Until you do this, `tauri:dev` will still run (icons aren't required
in dev), but `tauri:build` will fail.
