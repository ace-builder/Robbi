# Optional event extensions

The public runtime performs no automatic training capture. `robbi.activity` carries conversation context and optional event hooks; with no selected extension it creates no event worker, capture queue or media copies.

A trusted Python package may expose a factory under the `robbi.event_sinks` entry-point group. Install it separately and select its entry-point name in the private robot configuration:

```yaml
runtime:
  event_sink: my_extension
```

The factory receives `RuntimeConfig` and must return an object implementing `emit`, `model_request`, `sample_imu`, `bind_request`, `turn_for_request`, `status` and `stop`. Event calls must enqueue bounded work and return promptly. Extension exceptions disable further event delivery while conversation and robot control continue. Missing or invalid packages do not prevent boot. Do not perform network calls or unbounded disk work in an event callback.

Optional `register_web(app, runtime, feature_slots)` registers routes before the server starts. Optional `public_ui()` returns a same-origin module URL matching `/api/<extension-name>/panel.js`; its module exports `mount(root, lifecycle)` and must release handlers/work when the view unmounts. The normal dashboard authentication and request-origin checks apply to extension routes. No panel is imported when no extension is installed.

These packages run as trusted host code and are **not sandboxed Android-style apps**. Only install packages you trust. Ordinary app projects should use the restricted app SDK and owner permission system instead. The private collector package is maintained independently and is not part of this repository or release.
