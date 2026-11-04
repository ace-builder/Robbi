# Live awareness and purposeful reactions

Live awareness is an **experimental, owner-started** way for Robbi to notice
changes through cloud audio/video perception. It is off after every restart.
The default adapter uses **OpenAI GPT-6.1 Terra** (`gpt-6.1-terra`) for sampled
camera frames. Gemini Live remains available for microphone streaming.
Robbi keeps its existing conversation model,
memory, ElevenLabs voice, output orchestration and navigation controller.

## Try it

1. OpenAI uses Robbi's existing `providers` dependency extra. Gemini additionally
   needs the `live` extra. No large vision model is installed on the Pi.
2. In **Settings → Connections**, save an OpenAI API key, or reuse the configured
   key. Gemini uses its own key when selected. You do not need to
   change the main conversation provider. The key uses the existing private
   `.env.secrets` mechanism and never appears in session status or browser responses.
3. Open **Overview → Live awareness**. The editable model field defaults to
   **OpenAI · `gpt-6.1-terra`**. Choose Gemini Live for microphone streaming.
   Terra receives sampled images, not raw live audio/video.
4. Start with the default **60-second, camera-only observation**. Check the
   observations before enabling microphone streaming or autonomous reactions.
5. To experiment with reactions, enable **Allow short autonomous reactions**.
   Autonomy, heartbeat and deliberation must be enabled in Settings, and Robbi
   must be awake with available daily activity/model budgets. Pause, quiet hours,
   focus, filming and occupied outputs take precedence. Live awareness never
   enables motors, exploration, voice capture or autonomy settings automatically.
6. Use **Stop streaming** to end the experiment. The top bar indicates an active
   session even after navigating to another dashboard page. **Stop robot** also
   cancels streaming and pending reactions. Leaving the page does not extend the
   session or restart it; reloading the dashboard does not grant new consent.

Terra sends one low-detail frame per observation, at most once every three seconds,
with at most 20 observations per session. It uses no reasoning for latency,
256 maximum output tokens, no automatic retries, and `store=false`. API usage
is billable. Previous short observations provide continuity without image history.
Stop cancels in-flight requests, and missing camera data stops without a model call.
The observer has no action tools and uses the existing validated evidence pathway.
See [Terra capabilities](https://developers.openai.com/api/docs/models/gpt-6.1-terra).

Gemini microphone input uses the existing capture stream at **16 kHz, PCM16 mono**. If
voice capture is disabled, unavailable, or configured at another rate, video can
still work. Audio is suppressed during Robbi's own speech and voice processing.
The ordinary wake-word/STT conversation remains available. This experiment does
**not** replace it with wake-free, full-duplex cloud conversation.

## Behavior

The observer reports a short factual change, its salience, and whether it is new.
It has no motor, filesystem, app, memory-write or messaging tools. A meaningful
medium/high-salience observation waits in attention for at most 15 seconds. Once
claimed, its lifetime covers the bounded model response (at most 40 seconds), so
a slow answer is not discarded merely because the unused-evidence timer elapsed. Repeated
wording does not manufacture novelty by changing a timestamp. Low-salience noise
and unchanged scenes do not start robot decisions.

The observer also reports `engagement` (`none`, `greeting`, `question`, `attention`),
`directed_at_robot`, and a short `utterance` when speech was actually supplied.
A directed wave or greeting is a new social event even in an unchanged room;
explicit directed engagement takes precedence over contradictory low/unchanged
labels. Camera-only observations cannot supply spoken questions or transcripts.
Rephrasing the same ongoing greeting does not cancel an in-progress answer.
After a neutral interval, a new greeting can be acknowledged again.

The existing attention loop then uses relevant approved memory, recent user
context, its small episode journal and registered face names to choose one action:
stay quiet, express a face or an existing silent face animation, make a brief comment, observe, or request already
authorized bounded exploration. An **express** choice cannot speak. Perception
does not turn quoted speech or visible instructions into owner commands.

Enable **React to greetings, questions and meaningful changes** for speech and
faces. Enable **Allow small in-place body gestures** separately for the existing
`happy-wiggle`, `tiny-no`, and `curious-peek` routines. This permission lasts only
for the selected session; motors must already be commissioned and existing
sensor, envelope and Stop checks still apply. It does not authorize arbitrary
driving or dances. The robot should be on a clear, supported surface.
Ordinary ambient face activity can yield to live reactions. A held image remains
on screen while Robbi speaks. Overview shows whether reactions are observing-only,
waiting for engagement, blocked by activity/budgets, or preparing a response.

For a fresh, explicitly enabled live reaction, decisions are at least 8 seconds
apart and wait at least 3 seconds after recent human activity. Active speech,
recording, playback, filming, mapping, movement, focus and higher-priority outputs
still block unsolicited reactions. The existing daily model/attempt budgets
remain in force. Ordinary autonomy retains its configured longer cooldowns.
Exploration still needs its separate budget, commissioning and navigation gates.

Reaction results are discarded when their evidence expires, changes, the live
session ends, or human activity/Stop invalidates execution. The persistent episode
journal gives later decisions continuity; live observations themselves are held
in RAM and do not become durable personal memories.

## Following through, without a chatter loop

These improvements also apply to ordinary autonomy when Live awareness is off:

- A **working intention** connects a successful observation/exploration to its
  next decision. Robbi can look at something and then respond to what he found,
  without waiting for the normal minute-long decision cooldown. The dashboard's
  autonomy panel shows the current intention and phase.
- At most **two continuations** are admitted per intention. Each needs successful
  sensing and novel evidence, expires after 45 seconds, and consumes the existing
  daily budgets. The continuation cooldown is five seconds. Failed sensing,
  Stop, interruption or expiry never schedules an automatic retry. No pending
  intention is resumed after reboot.
- The prompt includes Robbi's actual recent speech and requested output history.
  His own speech is excluded from novelty detection, so he cannot trigger a new
  conversation with himself. Repeated autonomous sentences are suppressed for
  ten minutes, including across service restarts; explicit user conversations
  are unaffected.
- Harmless changes in punctuation and grammatical padding do not make a live
  description new. Negation, direction, numbers and word order remain significant.
  This is a lightweight filter, not a claim of semantic scene understanding.
- Fresh relative orientation and calibrated handling posture provide additional
  context. They do not establish an absolute location or infer a person. Camera
  descriptions older than 60 seconds are excluded from current visual evidence.
- Expressive decisions can use a small allowlist of existing registered silent
  animations, including curiosity, thoughtfulness, relief and smiles. They cannot
  select a motor gesture, soundtrack or arbitrary animation. Ordinary expressions
  have their own cooldown rather than consuming the long speech-check-in interval.

An intention marked **Requested** means output was submitted to the existing
executor; it is not a guarantee that a speaker or display physically completed it.
Normal output health and diagnostics still apply. Working intentions do not grant
new app, motion, network or owner permissions.

## Resource and privacy boundaries

- One cloud session at a time, 15–300 seconds, at most 20 observation windows.
- JPEG frames sampled at most once per second, width at most 480 pixels, quality
  60. One camera read can be pending; no video backlog or competing camera owner.
- Microphone capture only appends to a nonblocking tap. At most 16 KB / half a
  second of PCM is queued; stale audio is dropped instead of replayed late.
- Three-second input windows use Gemini's activity start/end protocol. Video and
  optional audio arrive incrementally; each window produces one short observation.
  This bounds inference cadence instead of asking the model on every frame.
- Window collection, cloud response and the existing robot decision/TTS add
  latency. This is responsive perception, **not** a real-time motor feedback loop.
- Network waits, messages, upload/download totals and response duration are
  bounded. A failed session stops visibly and requires an explicit restart; no
  unlimited reconnect loop. Motors and local sensor loops never wait on this API.
  Two consecutive responses without a valid observation also stop the experiment
  with a model-compatibility message instead of quietly consuming more requests.
- No raw camera frames, PCM, cloud audio output or transcripts are stored by this
  feature. The owner can inspect the last six short observations until restart or
  a new experiment. Usual autonomy episode records can contain a short reason.
- Selected media leaves the robot and is processed under the cloud provider's
  terms. Duration/turn/byte caps are **not a monetary guarantee**. Gemini may
  generate native audio even though this observer ignores it; provider billing
  can include that output. Usage counters are the latest provider report, not an
  estimated invoice. Use account spending controls when running experiments.

## Architecture and extending providers

`robbi.live_awareness.gemini` contains only protocol messages and validated
observation parsing. `session` owns lifetime, bounded buffers, camera scheduling,
networking and metrics. `robot` binds the existing device services and attention.
`web` applies the existing owner authentication and exposes Start/Stop/status.
The dashboard component is independent of the main conversation UI.

A second provider should implement the same narrow observation contract, not a
second action executor. OpenAI Realtime is another candidate: it supports live
audio and image inputs, so a camera integration would supply sampled images.
**An OpenAI live adapter is not included in this first experiment.** Full-duplex
cloud conversation would additionally need tested echo cancellation, interruption,
speech ownership and an explicit decision about retaining ElevenLabs output.

## Validation status

Offline tests exercise the real session with a fake WebSocket provider, including
tool validation, bounded media, cancellation, failure handling and integration
with the existing autonomy controller. Browser checks use mocked API responses.
These checks do not establish real Gemini model access, cloud latency, microphone
quality or Pi Zero 2 W CPU usage. Hardware and paid-provider acceptance remain to
be measured before enabling this on a deployed robot.

Official references checked October 28, 2026:

- [Gemini Live audio/video quickstart](https://ai.google.dev/gemini-api/docs/live-api/get-started-sdk)
- [Gemini Live WebSocket protocol](https://ai.google.dev/api/live)
- [OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [WebSocket client limits and timeouts](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html)
