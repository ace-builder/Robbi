# Develop a Robbi app

An app is an ordinary folder containing `app.yaml` and Python code. It can live in its own repository and be shared as a ZIP. Robbi reads the manifest without importing the app, generates its settings/tool forms, and adds enabled tools to the conversation catalog. Code loads only when a tool is called.

Keep executable app behavior inside that folder, with relative imports between modules. See [portable packages and metadata](APP_PACKAGES.md) for author information, exact-size icon/screen images, runtime requirements, safe archive preview, updates and reversible removal. Bundled apps use the same portable project format as independently installed apps.

The bundled projects are working examples:

Project | Example
--- | ---
`apps/calculator` | Pure local tools, bounded arithmetic, no network or robot dependencies
`apps/robot-status` | Read robot status through the SDK
`apps/quick-notes` | App-owned state, multiple tools and a custom dashboard
`apps/weather` | A replaceable network service with structured results
`apps/image-studio` | Provider configuration, generated image artifacts and robot display
`apps/sensor-check` | Both ToF channels, IMU snapshots, missing-device handling and optional screen summary
`apps/expressions` | Measured dance presets and cached LCD sprites through shared output ownership
`apps/tic-tac-toe`, `memory-colours`, `higher-lower` | Stateful voice games, rendered boards and outcome screens
`apps/kitchen-timers` | Named durable timers through the `robot.reminders` capability
`apps/checklists` | Reusable local checklists with numbered steps and screen summaries
`apps/decision-helper` | Offline coins, dice, option selection and shuffling

## Start, check and package

From the `robbi-v2` checkout, with its environment activated:

```console
python tools/apps/app_project.py new ../hello-app --id hello-app
python tools/apps/app_project.py check ../hello-app
python tools/apps/app_project.py pack ../hello-app --output ../hello-app.zip
```

`new` creates a manifest, `main.py`, `helpers.py` and README in a new directory. It never modifies an existing project. `check` validates the manifest and publish inventory without importing app code; it does not execute or prove the handler's behavior. `pack` creates a ZIP that can be imported through **App library → Add an app**. Imported projects stay disabled until you review and enable them. Both new-project creation and ZIP output refuse to overwrite existing paths.

The packer includes at most 256 files, 32 MiB expanded and 16 MiB compressed. It rejects links, junctions, escaping paths and special files. It skips `.venv`, `.git`, Python/tool caches, `node_modules`, `.env*`, `config/local.yaml`, credential/secret files and common private-key formats. The resulting JSON report lists exclusions. These are filename protections, not a secret scanner: inspect what you publish and never embed keys in source code or image assets. Keep large models and runtime data out of the project.

Dependencies are explicit deployment work. The importer never invokes pip or runs installation scripts. Prefer the standard library and Robbi's services; install any additional trusted dependency manually into Robbi's environment and document it in the app README. Native dependencies must support the target Pi/Python combination.

## Describe one tool

```yaml
api_version: 2
id: hello-app
name: Hello app
version: 1.0.0
description: Return a configurable greeting.
entrypoint: main:run
permissions: []
triggers:
  - "Say hello to friend"
  - "Give me a friendly greeting"
tools:
  - name: hello
    title: Say hello
    description: Return a greeting when the user asks this app to greet a person.
    examples:
      - "Say hello to friend"
    timeout_s: 5
    input_schema:
      type: object
      properties:
        name:
          type: string
          title: Name
          maxLength: 100
          default: friend
config_schema:
  type: object
  properties:
    greeting:
      type: string
      title: Greeting
      maxLength: 100
      default: Hello
```

Write tool descriptions around the user's intent, required information and side effects. Explain when the tool should be called and how ambiguous inputs should be handled. Trigger sentences and examples guide the language model; they are not exact-match commands or regular expressions. Enabled tools are automatically discoverable without editing a central switch statement. Disabled or invalid projects do not add executable tools.

The schema subset supports objects, arrays, strings, numbers, integers and booleans; properties, required fields, defaults, enums and length/value limits. Unknown fields are rejected. Keep schemas to five nested levels, at most 40 object fields, and bounded arrays. Use `title` and `description` for clean generated forms. String arrays appear as one item per line; structured arrays use JSON. An app may expose 1–12 tools with individual deadlines of 1–180 seconds.

Two optional field annotations keep generated forms concise. Set `x-order` to an integer from 0–1000 to choose presentation order (smaller values first), and `x-advanced: true` to put an infrequently used field in the collapsible advanced section. These annotations affect presentation only; the same schema validation and defaults still apply. Keep essential inputs and provider credentials discoverable. For example:

```yaml
provider:
  type: string
  enum: [open_meteo, met_no]
  default: open_meteo
  x-order: 10
timeout_s:
  type: integer
  minimum: 3
  maximum: 35
  default: 25
  x-order: 100
  x-advanced: true
```

## Implement the handler

`main.py`:

```python
from .helpers import greeting


def run(context, tool_name, arguments):
    context.checkpoint()
    return {
        "ok": True,
        "text": greeting(context.config["greeting"], arguments["name"]),
        "data": {"name": arguments["name"]},
    }
```

`helpers.py`:

```python
def greeting(prefix, name):
    return f"{prefix}, {name}!"
```

Use **relative imports** such as `from .helpers import greeting` between app modules. Robbi loads each project in its own module namespace, so two projects can both contain `main.py` and `helpers.py`. Keep module-level code cheap and free of network/device operations. For several tools, dispatch on `tool_name` inside your own app or delegate to modules; the host already validates the registered tool and arguments.

Return a JSON object of at most 64 KiB. Recommended fields are `ok`, human-readable `text`, structured `data`, and optional `artifacts`. Never return bytes, image base64, provider keys or unbounded API responses. Return `ok: false` with an actionable explanation for expected problems such as an ambiguous city. Raising an exception marks the invocation failed without stopping other apps or the robot.

## Use the SDK

API | Permission | Behavior
--- | --- | ---
`context.config` | None | Validated app configuration with defaults applied
`context.checkpoint()` | None | Raises when cancelled or past the deadline
`context.wait(seconds)` | None | Cancellable wait, at most 120 seconds and bounded by the current invocation deadline
`context.call_app(app_id, tool, arguments)` | `apps.read` plus the target's own permissions | Read a tool declaring `read_only: true`, with the target's saved configuration; at most eight calls, no recursive composition
`context.robot.listen_claps(duration_s=6)` | `robot.microphone` | Explicit 1–8 second capture from the live microphone stream; local clap times only, no saved audio or transcription
`context.request_permissions(*names)` | Requested capabilities | Check consent for several capabilities before any side effects; record missing requests together and end this invocation
`context.cancelled()` | None | Check cooperative cancellation without raising
`context.deadline` | None | Absolute `time.monotonic()` deadline to pass to adapters
`context.read_state()` | `state` | Read this app's JSON state, initially `{}`
`context.write_state(value)` | `state` | Atomically replace this app's state; object, at most 64 KiB
`context.robot.status()` | `robot.status` | Device health and current output ownership only; measurements/images/transcripts are excluded
`context.robot.tof(sensor="h1")` | `robot.tof` | Latest H1 or H2 snapshot; recomputes age, returns `distance_cm: null` when stale/unavailable
`context.robot.imu()` | `robot.imu` | Acceleration, gyro, quaternion and relative orientation with age/freshness/units; stale measurements are null
`context.robot.speak(text)` | `robot.speak` | Prepare up to 4000 characters, play, and return actual completion/unavailable receipt
`context.robot.capture_image()` | `robot.camera` | Return camera bytes or raise an actionable unavailable error
`context.robot.camera_snapshot(max_width=640, quality=75, max_age_s=2)` | `robot.camera` | Shared cached JPEG plus actual frame timestamp/age and dimensions; at most 640 pixels per dimension and 2 MiB
`context.robot.assets(kind)` | `robot.assets` | List bounded known `faces`, `sounds` or `videos`; no device access
`context.robot.play_sound(name)` | `robot.audio` | Play a named WAV and wait for completion; no silent queue replay after cancellation
`context.robot.play_sound_file("assets/chime.wav")` | `robot.audio` | Play this project's confined WAV file, at most 8 MiB
`context.save_image(bytes, title)` | `robot.display` | Fit a validated image onto a 280×240 white canvas and retain an artifact
`context.robot.show_image(artifact_id, hold_s=60)` | `robot.display` | Show a saved artifact; protect its idle presentation for up to 120 seconds
`context.robot.show_face(name, hold_s=15)` | `robot.display` | Show a known face with the same ownership rules
`context.robot.display_text(text, hold_s=15)` | `robot.display` | Render up to 500 characters with pixel-based wrapping; return saved artifact, display result and truncation flag
`context.robot.play_video(name)` | `robot.display`, `robot.audio` | Atomically reserve screen/soundtrack; return actual displayed-frame and playback outcome
`context.robot.move(direction=..., speed=25, duration=.5)` | `robot.motion` | Forward/backward/left/right, speed 1–50, duration .05–3 seconds; completion follows braking, existing motor/ToF guards remain mandatory
`context.robot.gesture(name, with_motion=True, with_audio=True)` | `robot.display`, plus selected `robot.motion` / `robot.audio` | Registered small dances with measured wheel receipts, pickup cancellation and an 18 cm planned center envelope
`context.robot.face_animation(name, fps=10, loops=1)` | `robot.display` | Cached 280×240 sprites, 1–30 fps, at most 30 seconds; reports measured delivery and skipped frames
`context.robot.timer(operation, **arguments)` | `robot.reminders` | Start/list/cancel named kitchen timers through the durable scheduler; exact ID cancellation cannot affect ordinary reminders
`context.robot.session(*resources, duration_s=15)` | Corresponding output permissions | Context manager reserving `display`, `audio`, `motion` atomically for at most 120 seconds; speech permission can reserve audio but cannot authorize arbitrary sounds
`context.services.images` | `network` | Shared `ImageService`; remote inference, bounded image response
`context.services.weather` | `network` | Shared `WeatherService`; bounded cache, locations, dates and units
`context.services.google` | `network` | Calendar `calendar_events`/`calendar_create`; Gmail `gmail_search`/`gmail_read`/`gmail_draft`; shared OAuth refresh and bounded REST requests
`context.services.search` | `network` | Tavily basic `search(config, query, count=5)` with source links and bounded snippets
`context.services.maps` | `network` | Optional Google `geocode(config, address)` and `route(config, origin, destination, mode="driving")` with actual API results

Image/weather access uses context-bound facades exposing `generate()` and
`forecast()`. Google, search and Maps methods use the same facades. Each call rechecks network consent, even if the app cached the
facade earlier. The facade supplies app cancellation/deadline automatically;
caller-supplied cancellation or an earlier deadline can further restrict it.
Late results after cancellation or revocation are discarded.

Declare only the permissions your app needs. For example, a stateful notebook uses `permissions: [state]`; an image app uses `permissions: [network, robot.display]`. Declarations do not grant access: the owner approves each capability in **App library → your app → App permissions**. First use records a pending request and ends the invocation. Granting access never replays an old request; ask the user to run it again. Grants and denials persist across restarts, and revocation cancels that app's outstanding SDK work. Permission requests do not mark the app broken.

Use `context.request_permissions("network", "robot.display")` before a paid image request, or preflight camera/movement/sound before beginning a scene. This collects missing approvals together and prevents discovering a missing permission halfway through the intended sequence. Do not catch permission exceptions and repeatedly retry. Optional capabilities should be requested only when that feature is selected.

The SDK enforces declarations and consent, but **Imported Python apps run in an OS sandbox**; bundled first-party apps retain the trusted in-process path. Imported apps use the supported SDK broker, with no direct host files, devices or network access. Custom pages have a separate restricted browser boundary and cannot approve permissions. For development, `apps.permissions_enabled: false` in the robot YAML bypasses declarations and consent after restart, without changing saved decisions. Hardware bounds and orchestration still apply. See [owner permissions](APP_PERMISSIONS.md) for storage, updates, authentication and recovery.

Use `text` for normal conversation responses; calling `robot.speak()` is only needed when an app deliberately produces robot speech itself. Missing hardware should be handled according to the feature: the image app still returns a saved preview if the display is unavailable. Do not import the robot controller or assume hardware exists.

Sensor snapshots read the existing sampler's cache, not the bus or the slower
dashboard diagnostics. `available` describes the component; `fresh`/`ok` describes
whether the measurement is usable now. Check it before using values. IMU yaw is
relative gyro orientation, not compass north. Camera snapshots contain binary
`data`; consume it within your app/provider or save an artifact rather than
returning bytes in a tool result. Apps cannot change sensor polling/calibration.

### Coordinate a short scene

All supported output methods use the same coordinator as manual controls,
conversation, mapping and background faces. Normal contention raises
`robbi.orchestration.ResourceBusy`, with `details` identifying the resource and
owner; an uncaught conflict becomes a structured `busy` tool result without
marking the app broken. Do not retry driving automatically. Cancellation,
preemption and Stop prevent late output, including work that was already queued.

Use a session when several outputs must be admitted together:

```python
# Requires robot.display and robot.audio. Prepare images/network data first.
context.request_permissions("robot.display", "robot.audio")
with context.robot.session("display", "audio", duration_s=8):
    shown = context.robot.show_face("happy", hold_s=0)
    played = context.robot.play_sound("happy")
return {"ok": played["ok"], "text": "Scene finished.",
        "data": {"display": shown, "audio": played}}
```

Calls in this example are sequential within an exclusive scene; the session does
not create threads or make them simultaneous. Keep network/model work, including
speech preparation, outside reserved scenes. Individual calls can claim free
outputs, but expanding an active scene into a conflicting output fails immediately.
An expired session cancels its intent and raises on exit. Static holds expire
without a timer thread; replacing a hold revokes the previous owner so its late
callbacks cannot overwrite the new screen. A natural hold expiry does not revoke
an otherwise active app. See [ownership policy](../development/HARDWARE_ORCHESTRATION.md).

`display_text()` retains a screen artifact, so use it for occasional summaries,
not a sensor polling display loop. Reuse saved image IDs for repeated content.

Example app-owned state:

```python
context.require("state")
state = context.read_state()
state["visits"] = int(state.get("visits", 0)) + 1
context.checkpoint()
context.write_state(state)
return {"ok": True, "text": f"Visit {state['visits']}"}
```

State/config live under Robbi's configured data directory, separately from project source. One queued or running invocation per app prevents concurrent handler writes. Do not create your own polling loops, background threads or executors for normal tool work. The host shares two app workers and two pending slots; one slow app consumes one slot instead of creating more threads. Cancellation is cooperative: Python cannot safely kill a blocked native operation, so every custom network/device operation needs its own timeout and cancellation checks.

## Settings and providers

Declare settings in `config_schema`; the host produces a form and validates updates. Settings changes take effect on the next invocation, once the app is idle. Store key **names**, not key values:

Configuration updates merge declared fields recursively. A `null` value removes
that override, restoring its schema default if one exists; required fields without
defaults still cannot be removed. This lets a user clear optional coordinates or
reset a numeric tuning value without editing files.

```yaml
api_key_env:
  type: string
  format: env
  title: API key environment variable
  default: MY_SERVICE_API_KEY
  maxLength: 100
```

The owner puts `MY_SERVICE_API_KEY=...` in Robbi's deployment environment or `.env.secrets`; updating the process environment requires a runtime restart. For shared image/weather services, pass `context.config`, `cancellation=context.cancelled` and `deadline=context.deadline`. See [provider contracts and examples](APP_PROVIDERS.md). The Image Studio example translates advanced JSON text settings into provider dictionaries before calling its service.

For other HTTP APIs, `robbi.app_services.http.HttpClient` is a lightweight reusable adapter. Declare `network`, call `context.require("network")`, supply a finite `timeout_s` and `max_bytes`, and pass the same cancellation/deadline. Never implement automatic retries of requests that might charge money or repeat a physical action.

## Optional custom dashboard

Generated forms are enough for many apps. To add a dedicated page:

```yaml
dashboard:
  title: My app
  description: A focused interface for this app.
  page: dashboard/index.html
```

The page must be a local HTML file of at most 256 KB. Local JS/CSS/image/font assets can be served from the project, up to 2 MiB each. A custom page runs in a sandboxed iframe with no direct network access; use the app-scoped bridge:

```html
<script src="/static/app-sdk.js"></script>
<script>
  async function greet() {
    await RobbiApp.ready;
    const {job} = await RobbiApp.call('hello', {name: 'friend'});
    // Poll RobbiApp.job(job.id) with a bounded interval until state is terminal.
    // The result is in job.result; a 202 response means accepted, not finished.
  }
</script>
```

The bridge also provides `RobbiApp.info()`, `.saveConfig(config)`, `.artifacts()`, `.job(id)` and `.cancel(id)`. It can access only the enclosing app and jobs that page started. Do not fetch other robot endpoints from the iframe or try to access the parent DOM. The complete Quick Notes page demonstrates bounded polling, escaped/text-only content, failure messages, keyboard-friendly controls and the white/grey visual style.

## Test and update

Test pure helpers normally. Test handlers with a small fake context and injected services; no robot or provider credentials are required. Verify malformed arguments, missing hardware/network, cancellation and state limits when relevant. The project's provider tests contain reusable examples of mocked API contracts.

Run `check`, package the project, then import the ZIP and enable it in the library. Importing an update replaces project code only when app calls are idle and disables the project for review again. App-owned data/config remain separate. For local development in an installed project folder, edit source and use **Scan app folders** while idle to clear its import cache; a full robot restart is unnecessary for ordinary app code changes.

Current setup and isolation contract: [security and review notes](../security-and-review.txt). Imported apps cannot bypass OS isolation with the SDK development permission switch.
