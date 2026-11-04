# Local household apps

These four V2 apps run locally without network providers or API keys. Open their generated forms in **App library**, or ask for the named action in a conversation. Each app has three tools and its own persistent records. Use returned record IDs for edits and removals; identical names can describe different objects, food batches or chores.

All four need the `state` capability. Approve it in the parent dashboard before saving. The optional photo and timer actions request their additional capabilities only when selected. Dates use the app's configurable IANA timezone, default `Europe/Berlin`, and strict `YYYY-MM-DD` calendar dates. Unknown timezones, impossible dates and arithmetic beyond years 0001–9999 are rejected.

| App ID | Tools | Record limits |
| --- | --- | --- |
| `where-did-i-put-it` | `remember`, `find`, `update` | 40 location records |
| `fridge-rescue` | `stock`, `suggest`, `shopping` | 60 stock batches and 60 shopping entries |
| `home-care` | `manage`, `agenda`, `act` | 40 recurring chores |
| `decision-notebook` | `save`, `score`, `review` | 8 decisions, each with 2–5 options and 1–5 criteria |

Each app also limits its saved JSON to 48,000 UTF-8 bytes and the SDK's 4,000-node complexity limit. Long notes or multibyte text can reach this limit before the record count. Rejected additions preserve the existing file. Records are validated before reads and edits: an unsupported version, unknown fields, invalid dates, duplicate IDs or malformed rows stop the action with a repair/backup message. No app silently resets malformed state or drops older records to make space.

## Where Did I Put It?

Say “Remember the spare keys are in the blue drawer,” then “Where did I put my keys?” `remember` stores the item name, the location supplied by the user, optional notes and the local update date. An optional existing `id` edits that record; omitted notes and an existing photo reference are preserved. `find` searches names, locations and notes without case sensitivity and can filter stored/found status. `update` marks an exact item found, marks it stored again, optionally updates its location, or removes its record.

`photo=true` explicitly captures the current camera view and saves a reference image. It preflights `robot.camera` and `robot.display` consent before capture. A photo does not determine or verify the object's location, identify objects or interpret image contents. Camera failure leaves the location record unchanged. Text-only saves need neither capability.

Photo artifacts use the existing SDK's 280×240 presentation canvas and retain at most 12 images per app. Older photo URLs may expire while their location records remain. Removing or undoing a record does not erase a retained image; SDK artifact retention manages the files. These images are reference thumbnails, not full-resolution photo storage.

## Fridge Rescue

`stock` lists, saves or removes food batches. Each batch has an ingredient name, optional quantity text, optional date, and fridge/freezer/pantry location. Multiple batches of the same ingredient have different IDs. Updating by ID preserves omitted fields; an explicitly empty date clears the saved date. The list can show all stock, dates within the next 0–30 days, or past-date entries.

`suggest` matches saved names against eight bundled preparation ideas: tomato pasta, vegetable rice, spinach omelette, potato hash, chickpea stew, tomato cheese toast, banana oats and courgette pasta. It recognizes a small documented-in-code set of common English plurals and German names, including `Nudeln`, `Tomaten`, `Zwiebeln`, `Kartoffeln`, `Eier`, `Kichererbsen` and `Haferflocken`. Other names need to match the recipe ingredient name exactly after trimming whitespace and ignoring case. Free-form descriptions such as “half a bag of red onions” belong in quantity text; they are not parsed into ingredients.

Each idea shows matched ingredients, missing ingredients, dates coming up within three days and simple preparation steps. Fully matched ideas sort first, then ideas using soon-dated ingredients. At least one stored ingredient must match. Past-date stock is excluded; stock without a date remains eligible. Quantities and serving sizes are not estimated. Dates are user-supplied labels, not a food-safety assessment. Ingredient exclusions filter exact canonical names and do not establish allergen or dietary suitability.

Suggestions never add groceries automatically. `shopping(action="add_missing", recipe_id="tomato-pasta")` explicitly adds the recipe's currently missing ingredients to **Fridge Rescue's own shopping list**. The current SDK has no shared-productivity-list facade, so this list is separate from Robbi's general shopping list. The same tool can add named items, list entries, mark them bought/needed or remove them by ID. Repeated additions deduplicate canonical names; adding a bought item reopens its existing entry. Marking a grocery bought does not create a stock batch or guess a quantity/date.

## Home Care Companion

Save a chore such as “Change the kitchen water filter every 30 days.” New chores default to seven-day intervals and today's due date when those values are omitted. Existing IDs update only supplied fields. Intervals range from 1 to 3,650 days. Changing an interval recalculates the next due date from the last completion, if present; an explicit `first_due` overrides that calculation. Notes and completion counts are retained.

`agenda` shows overdue tasks and a chosen future horizon. Set `include_all=true` to list every saved chore, including distant future dates. `act(action="complete")` records today or an explicit actual completion date and sets `next_due = completed_on + every_days`. A late completion therefore shifts the next due date. Repeating the same completion date does not increment the count or reschedule it. Future completions and dates before the latest completion are rejected; undo an incorrect completion before recording its replacement.

Due dates alone do not schedule audible alerts. `act(action="remind", id=..., minutes=20)` explicitly starts a one-shot timer using `robot.reminders`, from 1 to 1,440 minutes. The returned timer ID identifies that exact reminder; SDK undo cancels only that timer. Timers use the existing durable kitchen-timer scheduler and remain independent of the recurring chore's due date. Removing or completing the chore does not cancel an independently started timer. Use the reminder invocation's Undo action or Kitchen timers with its exact ID. There is no background chore polling, future-date alarm scheduler or automatic repeating notification.

## Decision Notebook

Save a title, 2–5 distinct option names and 1–5 distinct criteria. Optional integer weights 1–10 correspond to criteria in the same order; empty weights give equal importance. For example, options `Bike`/`Bus`, criteria `Comfort`/`Cost`, and weights `1`/`3` make cost three times as important. String arrays use one entry per line in the generated form; numeric weights use a short JSON list such as `[1, 3]`.

`score` assigns one option/criterion cell an integer from 0 to 10, with an optional reason. Higher always means better; score affordability positively instead of entering a raw price. Updating the same cell replaces its score and reason. `review(action="compare")` reports each option's reasons, weights and missing scores. It ranks options only after every cell is scored, using `sum(score × weight) / sum(weight)`. It reports ties explicitly and does not silently treat missing scores as zero.

`review(action="outcome")` stores the chosen option, a dated outcome review and notes about what happened. The chosen option may differ from the highest weighted score. Preference scores do not predict outcomes. List/search and remove actions use the same tool.

Editing a decision preserves omitted notes, existing weights when criteria stay unchanged, and current scores. Changing option or criterion names/order after recording scores or an outcome requires explicit `reset_scores=true`, which clears those ratings and the old outcome. A non-future review date is required. Large decisions remain subject to the shared saved-data bound above.

## Undo, follow-ups and verification

All state changes use `context.write_state`, so the shared invocation history, conversational follow-ups and conflict-aware undo work without app-specific host hooks. Later conflicting edits prevent overwriting newer data. Undo receipts are bounded and session-local; saving a record persists across restart, while its undo receipt does not. Optional timer starts register exact-ID compensation through the SDK. Camera artifacts are retained even when undo removes their record association, and history metadata reports that limitation.

Browser receipt scopes organize an owner's actions; they do not replace dashboard authentication. App records and any tool outputs included by the configured conversation/recording system may contain the user's notes and location information.

`tests/test_life_tools_apps.py` exercises the actual manifests, loader, AppHost, SDK permissions and state files with fake camera/timer ports and disabled HTTP. It covers CRUD and stable IDs, persistence, one-use undo, permission preflight, retained photos, exact timer cancellation, date and timezone boundaries, stock matching and explicit shopping additions, weighted comparison and reset consent, corrupt files, duplicate IDs, unknown versions, record caps and multibyte state/result bounds.
