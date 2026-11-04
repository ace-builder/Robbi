# Everyday utility apps in Robbi V2

These 13 bundled projects add a random number tool and 12 useful everyday apps.
They appear automatically in **App library** after a restart or **Scan app folders**,
with generated forms and voice tools. All run offline using Python's standard
library. Each project includes its own implementation and declares its Robbi SDK
requirements in app.yaml. No provider keys are required.

App ID | What it does | Example
--- | --- | ---
`random-number` | Inclusive integer ranges, repeated or unique draws | Pick five unique numbers from 1 to 50
`world-clock` | Current times and explicit date/time conversion | Convert 09:00 in New York on October 23 to Berlin
`date-calculator` | Date shifts, countdowns and weekdays with supplied holidays | How many weekdays until December 1?
`percentage-helper` | Percentage portions, changes, discounts and increases | Take 25 percent off 79.99
`tip-splitter` | Tips and exact rounded bill shares | Split 93 euros four ways with a 15 percent tip
`recipe-scaler` | Scale named ingredient amounts between portion counts | Scale four portions to six
`unit-converter` | Physical units, duration, area, energy, pressure and data sizes | Convert 500 square feet to square meters
`text-toolbox` | Counts, case conversion and whitespace cleanup | Count the words in this text
`password-generator` | Secure random passwords with selected character groups | Generate a 20-character password
`habit-tracker` | Daily completions and current/best streaks | Mark reading complete today
`expense-tracker` | Small local expense log and totals per currency | Log 12.50 euros for lunch
`packing-list` | Trip dates, categories, needed and packed quantities | Three of my five shirts are packed
`bookmarks` | Titled HTTP/HTTPS links with searchable tags | Find my walking bookmarks

## Dates and numerical conventions

Dates use `YYYY-MM-DD`. Clock conversion takes a complete local timestamp such as
`2026-10-23T09:00`, plus an IANA timezone. The current-time tool includes each
zone's date and UTC offset. Times that fall in a daylight-saving gap are rejected;
repeated times require an explicit earlier/later selection. The timezone database
comes from the operating system or the runtime's existing Windows `tzdata` dependency.

Date, habit and expense settings default to `Europe/Berlin`. An empty date uses
today in that zone where the field says so. Expense range filters include both
endpoints. Date differences include the earlier date and exclude the later date;
reversing a range negates its result. Business days exclude weekends and only the
holidays explicitly supplied, with duplicate holidays counted once.

Financial arithmetic uses `Decimal`. Bills and tips use half-up rounding, with
0-3 decimal places selectable for currency units; remainder units go to the first
people so shares add up exactly. Expense records always use two decimals. Currency
labels are independent: there is no foreign exchange, banking, tax or accounting
integration. Percentage change divides by the absolute starting value; change from
zero is undefined. General utility numbers are rounded to eight decimal places.
Intermediate decimal operations retain 60 significant digits; very large
percentage changes from tiny nonzero inputs remain displayable within the tool's
output budget. Calculator expressions use Python floating-point arithmetic and
show the returned precision rather than a six-digit summary. Invalid arithmetic,
syntax and unit combinations return an actionable tool result.

Recipe amounts scale linearly, preserving unit labels. Cooking times, temperatures
and seasoning need judgment. Unit symbols are case sensitive: `MB` is decimal,
`MiB` binary, `B` bytes and `bit` bits. US and imperial volume units are distinct.
Fixed-duration days mean 24 hours; months and years are deliberately not duration
units. Incompatible categories and temperatures below absolute zero are rejected.

## Local records and limits

Habit, expense, packing and bookmark apps request the `state` capability. Approve
it in the app's permissions panel and repeat the operation; permission approval
never replays a write. Records live under the runtime data directory in
`app-runtime/data/<app-id>/state.json`, separately from the project source. They
survive scans and restarts, and each app only accesses its own file.

App | Record bound | Behavior at the limit
--- | --- | ---
Habit tracker | 12 habits, 120 completion dates per habit | Remove a habit or unmark an older date; no automatic pruning
Expense tracker | 120 records | Remove an expense by its exact ID
Trip packing | 5 trips, 40 rows per trip, 1-100 units per row | Remove an item or delete an old trip
Bookmarks | 40 links, 5 tags per link | Remove a link by its exact ID

The apps also check a 55 KB serialized-state budget before writing, leaving room
within the SDK's 64 KiB limit. Long Unicode descriptions/URLs may reach that budget
before the record count. Existing state is preserved when an operation fails.
Back up the runtime data directory to protect saved records. Corrupt records are
reported instead of silently replaced.
Duplicate expense/bookmark IDs and duplicate saved bookmark URLs are treated as
invalid state, so an ambiguous removal or update cannot modify multiple records.

Habit marks and packed quantities are idempotent. A habit's current streak may end
yesterday while today remains open; best streaks describe only retained dates.
Habits do not create reminders. Packing tracks absolute quantities and trip dates;
item numbers can shift after a removal. Saving the same exact bookmark URL updates
its title/tags. Bookmarks never fetch or open websites, and reject embedded URL
credentials. Expense additions create new entries, so do not repeat a successful
add command to retry a spoken response.
Bookmark tags are case-normalized and must still fit 30 characters afterward;
expanding Unicode mappings that exceed the limit are rejected before saving.
Bookmark search includes all matching records in structured results, with at most
eight titles in the spoken summary to keep a full collection within output limits.

## Randomness, text and output handling

Random integers use operating-system randomness, with bounds from -1 trillion to
1 trillion and at most 100 results. Unique draws fail when the range is too small.
Password generation uses `secrets`, 12-128 characters and at most five results.
Every selected character group is represented. **Passwords appear in tool results
and may be retained in Robbi's job or conversation history.** The password app
does not save its own state, and it is not a password vault.

Text tools accept up to 4000 characters. Words are whitespace-separated tokens;
characters are Unicode code points, not visual grapheme clusters. Case mappings
are Unicode-aware, not language-specific. Long expanding transformations are
rejected to keep tool output bounded. All tools run without scheduling background
jobs or accessing robot hardware.

## Verification

Run `python -m pytest tests/test_utility_apps.py` from `robbi-v2`. The tests scan
the real projects and invoke them through AppHost, validating default forms,
timezone transitions, arithmetic edge cases, randomness constraints, permission
behavior, persisted state across new hosts and malformed inputs without HTTP I/O.
