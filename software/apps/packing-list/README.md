# Trip packing

Plan trip quantities by category and record exactly how many of each item are packed.

Use the generated forms in Robbi V2 App library, or ask Robbi using the example phrases. Its implementation travels inside this project; it requires only the declared Robbi SDK and no provider keys or network connection.

Approve the state capability on first use, then repeat the command. Records live in this app's state.json under the configured runtime data directory and survive restarts.

At most five trips, each with 40 item rows and quantities from 1 to 100. Departure is an optional YYYY-MM-DD label. Packing sets an absolute quantity, so retries do not double it. Item numbers can change after removal; show the list again before editing. Trip deletion removes all its items. No travel advice or automatic reminders.
