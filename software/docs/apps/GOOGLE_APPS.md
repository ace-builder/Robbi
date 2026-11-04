# Google Calendar and Gmail for Robbi V2

Both apps appear in **App library** with generated forms and voice tools. Calendar
lists/searches appointments and creates timed events. Gmail searches and reads
messages and saves drafts for review and sending in Gmail. Credentials are
configured once; both apps can share the same Google account.

## Connect an account

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select
   your project and enable **Google Calendar API** and **Gmail API** as needed.
2. Configure the Google Auth Platform consent screen/audience. For a personal
   test project, add your Google account as a test user. Create an OAuth client
   with application type **Desktop app** and download its JSON file. Keep that
   file outside the repository and app folders.
3. From the Robbi V2 checkout, run this on the desktop computer whose browser you
   will use. Pick a new private output path; the helper refuses to overwrite it:

   ```console
   python tools/accounts/google_oauth_setup.py --client-secrets /path/to/client_secret.json --output /private/path/robbi-google.env --calendar write --gmail draft
   ```

   Windows example (PowerShell):

   ```powershell
   python tools/accounts/google_oauth_setup.py --client-secrets "$env:USERPROFILE\Downloads\client_secret.json" --output "$env:USERPROFILE\robbi-google.env" --calendar write --gmail draft
   ```

4. Review the requested permissions in Google's browser flow. The helper uses a
   random loopback port, PKCE and a checked `state`. Sign-in must happen on the
   same machine as the helper. No calendar events, drafts or emails are created
   during setup. The terminal never prints the resulting credentials.
5. Privately transfer/merge the three generated variables into the robot's
   existing `robbi-v2/.env.secrets`, preserving its other variables, and restart
   Robbi. The development entrypoint and supplied systemd service load this file.
   Restrict file access to the Robbi account (`0600` on Linux; user-only file
   permissions on Windows). The helper creates files with `0600` on POSIX;
   Windows inherits the destination folder's access controls.
6. In **App library → Google Calendar/Gmail → App permissions**, grant `network`.
   In Calendar settings choose your IANA timezone, for example `Europe/Berlin`.
   The default calendar ID `primary` selects your main calendar. Run a read tool
   to verify access. A pending permission request must be run again after grant.

The generated environment variables are:

```dotenv
ROBBI_GOOGLE_CLIENT_ID=<desktop-client-id>
ROBBI_GOOGLE_CLIENT_SECRET=<desktop-client-secret>
ROBBI_GOOGLE_REFRESH_TOKEN=<refresh-token>
```

These are placeholders, not working credentials. App settings contain the
**names** above; do not paste their values into the dashboard or app YAML. The
service retrieves access tokens lazily and caches them in memory until shortly
before expiry. Concurrent Calendar/Gmail calls using the same credentials share
one refresh while keeping their own cancellation and time limits. An authorization
failure only invalidates the affected token, preserving other connected accounts.
The service never persists access tokens. Changes to the environment need a
runtime restart.

## Choose account access

Helper option | OAuth scope | Available features
--- | --- | ---
`--calendar read` | `calendar.events.readonly` | Read/search events
`--calendar write` | `calendar.events` | Read/search and create events
`--calendar none` | None | Skip Calendar access
`--gmail read` | `gmail.readonly` | Search/read mail without changing read status
`--gmail draft` | `gmail.readonly` + `gmail.compose` | Search/read and create drafts
`--gmail none` | None | Skip Gmail access

Scope names above have the prefix `https://www.googleapis.com/auth/`. The helper
defaults to read access for both apps. For Calendar only, choose `--gmail none`.
Google's `gmail.compose` scope also permits sending at the account level, but
Robbi has no send tool or send API call. There is no narrower general Gmail scope
for creating drafts. Scope definitions are in Google's
[Gmail authorization reference](https://developers.google.com/workspace/gmail/api/auth/scopes).

To add capabilities, rerun the helper with a new output filename and replace the
three environment values. To remove previously granted access, revoke the app
in your Google account's connections, then reconnect with the desired scopes.
For separate accounts, run setup separately and give each app different variable
names in its settings.

External OAuth apps in **Testing** commonly receive refresh tokens valid for
seven days when these scopes are requested. Revocation, account changes and
Google policy can also invalidate tokens. Gmail read/compose are restricted
scopes; distributing a public app may require Google's verification process.
Workspace administrators can block account access. See Google's
[OAuth lifecycle guidance](https://developers.google.com/identity/protocols/oauth2#expiration)
and [desktop OAuth setup](https://developers.google.com/identity/protocols/oauth2/native-app).

## Use the tools

- Calendar: “What is on my calendar tomorrow?” supplies that day's start and end
  in your timezone. An empty range means now through the next 30 days. Search is
  bounded to 10 events; refine dates or keywords if more results exist. Recurring
  occurrences are expanded, and all-day events retain their date values and
  exclusive end date.
- Calendar creation: provide a title, start and end, for example
  `2026-10-23T14:00:00+02:00` through `2026-10-23T15:00:00+02:00`. A local timestamp
  without an offset uses the configured timezone. Daylight-saving gaps and
  repeated hours require an explicit offset. Attendee invitations, recurring
  series creation, edits and deletions are not exposed.
- Gmail search: queries such as `in:inbox is:unread`, `from:maker@example.com` or
  `newer_than:7d` return up to 10 summaries with IDs. Use an ID for Read message.
  Google's date-only `after:`/`before:` queries use its documented PST date
  semantics; use Unix timestamps for exact boundaries. See
  [Gmail search semantics](https://developers.google.com/workspace/gmail/api/guides/filtering).
- Gmail drafts: enter To, subject and plain-text body; Cc is optional. Recipients
  are comma-separated bare ASCII email addresses, without display names or
  quoted local parts. Plus addressing and punycode domain names are supported.
  Malformed dot placement, domain labels and overly long address components are
  rejected before sign-in or draft creation. Open the resulting draft in Gmail
  to review and send it yourself.

API requests have bounded response sizes and a shared per-tool time budget. Read
messages return up to 6000 characters, plus attachment names; HTML becomes plain
text and external images/attachments are never fetched. Search uses metadata
reads, so opening the inbox does not download full bodies. Messages too large for
the 512 KiB read response limit must be opened in Gmail. Within MIME alternatives,
readable plain text is preferred, with HTML as a fallback. Independent multipart
sections remain in order; related resources and attached forwarded emails are
excluded from the body. At most 100 MIME parts, 20 nested container levels and
10 attachment names are processed. A partial preview is identified in the result,
including when HTML trimming would otherwise hide that text was omitted. MIME
handling follows the [Gmail message structure](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages#MessagePart),
[multipart rules](https://www.rfc-editor.org/rfc/rfc2046.html#section-5.1) and
[related-part root rules](https://www.rfc-editor.org/rfc/rfc2387.html#section-3).
App job history and conversation context can contain
the requested mail/calendar content; protect the dashboard and runtime data like
other personal information.

Calendar creation and draft creation are remote writes. A timeout/cancellation
cannot undo a request Google already accepted. Nothing is automatically retried;
check Calendar or Gmail Drafts before repeating an uncertain write.

## Troubleshooting and API references

- **Missing environment variable:** copy the generated credentials into the
  runtime environment and restart; confirm the names in each app's settings.
- **Sign-in refresh failed:** reconnect with the helper; check the client JSON,
  test-user status and token expiry. The error never includes token values.
- **403/denied access:** enable the relevant API and reconnect with the required
  scopes; check Workspace policy and write permission on the selected calendar.
- **No matches:** narrow/adjust the date range or Gmail query. Empty results are
  successful reads, not authentication errors.
- **Wrong time:** set Calendar's timezone and provide explicit ISO offsets for
  ambiguous timestamps.

Implementation contracts: [Calendar events list](https://developers.google.com/workspace/calendar/api/v3/reference/events/list),
[Calendar event creation](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert),
[Gmail message list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list),
[Gmail message read](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get),
[Gmail draft creation](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/create).
