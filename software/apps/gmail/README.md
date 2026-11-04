# Gmail

Search your Gmail inbox, read message text and save drafts through Robbi's voice
tools and generated dashboard. Requires `network` permission and a Google OAuth
connection. Follow `docs/GOOGLE_APPS.md` and run `tools/google_oauth_setup.py` from
the Robbi V2 checkout. Settings store environment variable **names**.

Search/read requires `gmail.readonly`; creating drafts additionally requires
`gmail.compose`. Google describes the latter as a scope that also permits sending;
Robbi exposes only draft creation and has no send endpoint. Review and send your
draft in Gmail. The app never changes read/unread labels or deletes mail.

Use Gmail queries such as `is:unread newer_than:7d` or
`from:person@example.com subject:meeting`. Up to 10 results include message IDs;
paste an ID into Read message. Plain-text previews are at most 6000 characters,
with a shorter spoken summary. HTML is converted to text; attachments and remote
images are never fetched. Large messages beyond the 512 KiB response bound should
be opened directly in Gmail. Google date-search semantics follow Gmail's API;
use explicit Unix timestamps in `after:`/`before:` for exact timezone boundaries.

Drafts accept up to 10 comma-separated addresses per To/Cc field and plain-text
bodies. All network work has a shared deadline and no automatic retries. If draft
creation times out or is cancelled, check Gmail Drafts before trying again.
