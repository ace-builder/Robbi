# Connections, web search and wake training

## Provider setup in the dashboard

Open **Settings → Connections & API keys**. Select OpenAI, OpenRouter, Alibaba,
Gemini, Custom/local, or Tavily. Paste a key, enter a model ID if applicable,
and save. Empty key input keeps the existing key. Keys are write-only: the
dashboard returns a configured indicator, never the stored value. They are
saved in the installation's private `.env.secrets` with owner-only permissions.
Saving applies immediately; restarting is unnecessary.

Use **Find models** to request the provider's model list, or type any exact model
ID it supports. **Test connection** sends a small request and checks whether a
language model follows Robbi's JSON response format. It never executes the
returned actions. Provider usage charges and quotas still apply. Passing this
test does not guarantee a small model can reliably follow the entire tool catalog.

Select **Use for typed and spoken conversations** when you want to switch both
conversation routes. Leave it unchecked to save a key without switching models.
Settings → LLM still offers separate typed/spoken routes. Background Work can
inherit those routes or use a separately chosen provider/model.

For Ollama, use `http://YOUR_LLM_COMPUTER:11434/v1`; for LM Studio,
`http://YOUR_LLM_COMPUTER:1234/v1`. The address must be reachable **from Robbi**.
`localhost` would mean the Pi itself. Models run on the external computer;
installing a large language model on the Zero 2 W is not part of this setup.
Custom/local supports OpenAI-compatible Chat Completions, not arbitrary vendor
request formats. Local HTTP endpoints are allowed; cloud endpoints use HTTPS.
See [Ollama compatibility](https://docs.ollama.com/api/openai-compatibility) and
[LM Studio compatibility](https://lmstudio.ai/docs/developer/openai-compat).

## Tavily web search

Select **Tavily web search**, paste the API key, select **Enable web search**,
then save and test. The environment variable is `TAVILY_API_KEY`. No key is
included with Robbi. The built-in tool and the standalone Web Search app share
one bounded adapter. The app retains its separate owner-approved network permission.

Try: “What are the latest science news stories from the past week? Give me two
findings with sources.” Current-news requests receive an immediate acknowledgement
while the model and search run in the background. The finished answer appears in
Interact. A request made by voice can finish speaking during that session once
outputs are idle. Muting, Stop and filming still take precedence. Old results
are never spoken again after a restart, and unsolicited task announcements stay
disabled by default.

The tool supports `query`, `max_results` (up to eight returned sources),
`search_depth`, `topic` (`general`, `news`, `finance`) and optional `time_range`
(`day`, `week`, `month`, `year`). Basic depth is the default. Sources carry URLs,
publication metadata when supplied, and a retrieval timestamp. Snippets are
untrusted evidence, not instructions. Robbi may make one refined search if
results are irrelevant or stale; it cannot enter an unlimited search loop or
use search output to trigger physical actions. Failed credentials are reported
without exposing keys or raw provider error bodies. If no reliable result is
available, Robbi should say so rather than invent a current event.

Reference: [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search).

## Personal wake calibration in the browser

Open **Wake word**. Record on Robbi's microphone, listen to the saved examples,
and delete unclear ones. The recorder waits two seconds before capturing.

1. Record at least four positives: say “Hey Robbi” once per clip, varying tone
   and normal speaking distance. Six to eight clean examples are a useful start.
2. Record at least three negatives: ordinary speech and similar-sounding words
   without the wake phrase.
3. Record at least two noise clips: the room, fan, TV or other realistic noise,
   without saying the wake phrase.
4. Click **Train in this browser** and keep the tab open. Robbi prepares compact
   MFCC features; a Web Worker on the controlling device performs DTW matching
   and threshold fitting. No cloud training service receives these recordings.
5. Review the clip-level hit and false-trigger counts. **Apply calibration** is
   enabled only when at least 75% of the checked positives match and no checked
   negatives trigger. Failed calibration leaves the current detector unchanged.
6. After applying, test fresh speech and noise that were not in the training set.
   Small datasets and sampled windows cannot guarantee real-world accuracy.

This is a **personal template detector**, not browser training of a neural
NanoWakeWord model. It calibrates `local_wake`, excluding a positive clip's own
template when checking it. Applying selects that engine and saves the threshold;
it does not turn on voice listening if you previously disabled it. Follow-up
listening remains disabled by default.

Preparation is bounded to one cancellable job, 32 positive / 24 negative /
16 noise clips, eight active templates, 8 MiB of feature data and three minutes
of processing. Existing 0.2–16-second mono 16-bit PCM clips are supported within
the file-size budget; new dashboard clips are limited to four seconds. At most
12 live-style windows are checked per clip. Dataset or relevant setting changes
invalidate the prepared result. Actual Pi preparation and browser matching times
are shown after every run.

## Full neural wake-model training

The advanced section exports a private dataset ZIP and imports a trained ONNX
model. Extract the ZIP on a computer, use the generated `training/config.yaml`
with a compatible [NanoWakeWord trainer](https://github.com/arcosoph/nanowakeword),
then upload its ONNX output. Select the model and engine in Settings → Voice;
test recognition before leaving it enabled. The exported README describes the
trainer command. Keep your dataset and trained personal voice files out of Git.

The browser calibration timing is **not** an estimate for neural training.
Full training depends on architecture, augmentation, training steps and hardware;
we have not benchmarked it on a Zero 2 W and do not promise a five-to-ten-minute
run. Use a separate computer for that workflow. The lightweight browser workflow
does not install PyTorch or training dependencies on the Pi.


### Conservative spoken follow-up

After successful speech playback, Robbi waits five seconds for a reply by default.
Change **Settings → Conversation → Listen briefly after replying** or the timeout
to suit the room. Existing owner configuration overrides the new defaults.
The normal first interaction still starts with the wake word.

Follow-up capture requires stronger, sustained microphone energy than explicit
wake-word input. Brief clicks, empty transcripts, filler and likely speaker echo
are discarded locally. One remaining candidate is transcribed and checked against
the last successfully spoken reply using the configured speech conversation LLM.
This small, tool-free check has a three-second provider timeout and no retries;
only a strict positive decision (confidence at least 0.9) reaches conversation or
tools. A short contextual answer such as “yes” or “and tomorrow?” can qualify.
Silence, ambiguity, provider failure or rejected input returns to wake-word mode
without any retry prompt. Once speaking has begun within the window, recording
may continue up to the existing command length limit.

The check runs off the microphone loop and uses no additional local model. It
can add a small LLM request and latency per candidate; captured candidates go to
your configured STT/LLM services. Rejected transcripts are not published to the
conversation feed or retained in conversation memory/debug trace payloads.
This is contextual filtering, not speaker identification: a nearby TV or another
person can still sound like a relevant reply. Use wake-word-only mode in noisy
rooms. Acoustic tuning and model accuracy still require testing on the robot.


Follow-up robustness: a lightweight acoustic check rejects constant fan/hum,
DC faults and very brief impulses before transcription. Short voiced replies can
qualify without a full sentence; uncertain transcripts still end silently. A
pending listening window expires if it cannot open within ten seconds, and a
late result cannot reset the state of a newer command. Microphone uploads stay
in memory instead of creating temporary speech files on the SD card.

Developer replay: `python tools/qa/followup_audio.py --manifest cases.json --out /tmp/robbi-followup-qa`
feeds actual PCM through the capture state machine without providers or robot
outputs. The JSON manifest is a list of objects containing `id`, `path` (a PCM16
WAV), `text` (expected words), and `reply` (Robbi's preceding spoken reply).
Install the test extras for NumPy/SciPy. It creates clean, reverberant, noisy,
muffled, clipped and distant versions plus synthetic noise controls. Keep the
output directory and any private/cloned speech outside your public source tree.
Only code and reproducible synthetic-noise tests belong in the public package.
The replay collects candidate captures; it does not claim to recognize speech.
Live STT/addressedness evaluation is a separate explicit, budgeted operation.
