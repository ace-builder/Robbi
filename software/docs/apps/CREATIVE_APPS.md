# Creative and personal tools

These ten apps are included in the 40-app library. Open **App library**, select
an app and grant its requested permissions. Saved records start empty. Generic
study tools do not record training datasets or train an LLM.

| App | Purpose |
| --- | --- |
| Ready to Leave | Combine a selected Calendar appointment, travel buffer, carry list, weather and optional departure reminder. |
| Where Did I Put It? | Save and search item locations, with optional reference photos. |
| Fridge Rescue | Track ingredients and dates, match curated offline recipes, and keep an app-owned shopping list. |
| Home Care Companion | Track recurring care tasks and completion dates; optionally request a one-shot audible reminder. |
| Meeting Prep | Build an editable brief from a selected Calendar event, related Gmail summaries and your notes. |
| Teach Me Something | Explore twelve short offline lessons and create study cards with spaced recall reviews. |
| Decision Notebook | Compare options using explicit criteria and ratings, then record choices and later outcomes. |
| Rhythm Relay | Follow clap patterns using a browser tap pad or optional short microphone capture. |
| Tiny Detective | Solve twelve mysteries with clues, an evidence notebook and saved progress. |
| Robot Charades | Play twenty prompts with expressions, sounds and optionally enabled movements. |

Ready to Leave and Meeting Prep reuse the [Google](GOOGLE_APPS.md), weather and
Maps settings. Grant access to the planning app and the underlying apps. Granting
a permission never replays an earlier request; run it again when ready.

Apps run offline where their function allows it. Item locations come from the
user, not visual recognition. Fridge dates are labels; the app cannot judge food
safety. Teach Me uses its bundled lessons and user-authored cards, rather than
generating arbitrary courses. Charades movement is disabled by default.

Use **Open dashboard** for game controls. Rhythm Relay can detect sharp sounds
from at most eight seconds of enabled microphone input, without saving or
transcribing audio. Noise can resemble claps; the browser tap pad remains an
alternative. Saved progress can be undone where supported; sound already played
and movement already completed cannot be undone.

See [app development](APP_DEVELOPMENT.md) for the API, standalone projects,
permissions, tool descriptions and custom dashboard pages.
