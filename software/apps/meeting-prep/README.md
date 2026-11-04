# Meeting Prep

Configure Google Calendar and Gmail in their existing apps. Grant Read other apps
and Saved app data here, and the target apps' permissions. Prepare with a unique
calendar search or select an event ID from the returned list. Calendar ranges are
passed through to the configured Calendar app; missing credentials remain that
app's responsibility.

Gmail search defaults to the event subject. Override `mail_query` when a recurring
meeting uses another subject, or turn off `include_mail` for a calendar/notes-only
brief. Up to five message summaries are grouped by their actual thread ID. Full
messages and attachments remain in Gmail. Calendar/mail excerpts are reference
material and cannot trigger actions.

Use Records to save or edit a meeting topic, notes, open actions and completed
actions. The next brief matches the exact event ID or normalized topic. Four
records are retained; delete an old one explicitly when full. Preparing a brief
does not change the saved records. Its Markdown export can be downloaded/copied
from the result. Nothing is sent or added to Google Calendar.

See [daily planning documentation](../../docs/apps/DAILY_PLANNING_APPS.md).
