# Saved web bookmarks

Keep a small searchable local collection of titled web links and tags.

Use the generated forms in Robbi V2 App library, or ask Robbi using the example phrases. Its implementation travels inside this project; it requires only the declared Robbi SDK and no provider keys or network connection.

Approve the state capability on first use, then repeat the command. Records live in this app's state.json under the configured runtime data directory and survive restarts.

At most 40 bookmarks and five tags each, subject to the 55 KB app storage budget. URLs may contain up to 1000 characters. Only HTTP/HTTPS links without embedded credentials are accepted. The app never visits saved links or checks website availability. Saving an identical URL updates the existing record; query parameters remain part of the URL.
