# Ready to Leave and Meeting Prep

These apps compose existing configured apps through `context.call_app`. Their
`apps.read` permission allows only tools declared read-only; the target app must
also be enabled and have its own permissions. Calendar, Gmail, Weather and Maps
keep their own configuration and credentials. Neither planning app copies OAuth
tokens or creates a second Google connection. Missing permissions end the request
without replay after approval. Child calls inherit cancellation and deadlines.

## Ready to Leave

1. Configure Google Calendar and Weather in those apps. Set Ready to Leave's IANA
   planning timezone; it defaults to `Europe/Berlin` and controls the departure
   display and the forecast date.
2. Save an origin, usual travel minutes, arrival buffer and selected carry items
   with **Departure preferences and saved plans → save**. Partial updates retain
   omitted fields; supplying carry items replaces that list.
3. Calculate departure with a calendar query or an exact event ID. If several
   appointments match, the app returns choices without selecting one. Narrow
   `time_min`/`time_max` when the desired event is outside the first ten results.
4. Optionally start a departure reminder using the exact returned plan ID. Planning
   never creates a timer automatically.

A manual travel time and a Google Maps directions link need no Maps API key. A
link contains no travel-time estimate, so the app leaves departure uncalculated
until travel minutes are supplied or a route estimate succeeds. The optional
`route_estimate` field uses the already configured Google Routes API, which may
incur charges. Driving estimates exclude live traffic. Provider attribution,
route warnings, missing forecast data and stale-cache warnings remain visible.

Departure is the event start minus elapsed travel time and the arrival buffer.
Arithmetic uses UTC before converting to the planning timezone, including across
daylight-saving transitions. All-day events, past appointments, invalid times and
events without a physical destination need clarification. A departure time that
has already passed is clearly flagged; its reminder is refused.

Preferences and the last three plans are stored locally. New plans replace the
oldest plan only when that three-plan limit is reached. Saved plans are snapshots;
recalculate after an appointment or travel estimate changes. Planning state writes
use the host's normal undo receipts.

The separate reminder tool accepts departures within the next 24 hours, starts a
durable named Kitchen timer, and saves its exact returned ID. It reserves the
request before scheduling, so an uncertain or repeated attempt cannot silently
create another timer. Check Kitchen timers if no exact receipt came back. Cancel
addresses only that plan's confirmed timer ID. Undo of a confirmed start cancels
that exact timer and restores the previous plan state. Physical reminder playback
that has already happened cannot be reversed.

## Meeting Prep

**Prepare meeting brief** selects a calendar event using the same exact-ID or
unambiguous-search rules. By default, it searches Gmail for the quoted event
subject, groups up to five message summaries by their actual thread ID, and links
to those threads. Set `mail_query` when the subject differs, or disable
`include_mail` for a calendar/notes-only brief. Gmail bodies and attachments are
not downloaded by this app. No email is sent, no draft is created, and no calendar
event is changed.

**Saved meeting notes and actions** can list, save, edit and remove up to four
records. Supply an existing record ID to edit it; omit the ID to create a record.
A new record needs a topic. Each record stores up to 1,500 characters of notes,
six open actions and six completed actions. Supplied lists replace those fields;
omitted fields remain unchanged. An action marked completed is removed from the
open list. Remove an old record explicitly before creating a fifth one.

A brief includes up to three previous records matching either the exact event ID
or the normalized topic, with outstanding actions and a short preparation
checklist. It uses the first 100 characters of a long event title as the default
topic; an explicit topic can group recurring meetings under a stable name. Saved
notes belong to this app; Robbi's separate Notes and long-term memory are not
searched automatically.

The result includes `data.export` with `filename`, `media_type` and Markdown
`content`, available through the dashboard's download control. Export content is
bounded to 24 KB and carries an explicit truncation notice when shortened. Source
excerpts are quoted, HTML-escaped and Markdown-escaped. External calendar/mail
content stays reference data and cannot authorize tools or sends. Preparing a
brief does not store mail excerpts or modify saved notes. Missing Gmail access is
reported while retaining the calendar and local-note brief.

## Validation and storage

Both apps validate stored records before every edit and refuse to overwrite
malformed state. State stays within the SDK's 64 KiB limit, including maximum
four-byte Unicode input. State lives at
`<data-dir>/app-runtime/data/<app-id>/state.json`. Restore a backup or repair an
invalid state file rather than automatically discarding it.

The acceptance tests use real manifests, AppHost, child app calls, permissions,
undo and local state with injected provider and timer adapters. They need no
connected accounts or live network access:

```console
python -m pytest tests/test_daily_planning_apps.py -q
```
