# Google Calendar

Find upcoming appointments, search a date range, or create an event from Robbi's
voice tools and generated dashboard. Requires a Google OAuth connection; use
`tools/google_oauth_setup.py` in the Robbi V2 checkout and follow
`docs/GOOGLE_APPS.md`. The app needs the `network` capability.

Settings contain environment variable **names**, never credential values. Select
your timezone, for example `Europe/Berlin`, and keep `calendar_id: primary` for
your main calendar. Read access needs `calendar.events.readonly`; creating events
needs `calendar.events` instead. Recurring events are expanded when reading.

The search defaults to the next 30 days and returns at most 10 appointments.
Provide both ISO date/time boundaries for a particular day or historical range.
For event creation, enter the title, start and end. Timestamps without an offset
use the configured timezone; ambiguous or nonexistent daylight-saving times need
an explicit offset. All-day events can be read; creation supports timed events.
No attendee invitations, edits, deletions or background synchronization are exposed.

API requests are bounded and are never automatically retried. If a create request
times out or is cancelled after reaching Google, check your calendar before
repeating it: cancellation cannot undo a remote event already created.
