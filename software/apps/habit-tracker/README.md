# Habit tracker

Record daily completions and see current and best streaks for your habits.

Use the generated forms in Robbi V2 App library, or ask Robbi using the example phrases. Its implementation travels inside this project; it requires only the declared Robbi SDK and no provider keys or network connection.

Approve the state capability on first use, then repeat the command. Records live in this app's state.json under the configured runtime data directory and survive restarts.

At most 12 habits with 120 completion dates each. No dates are silently discarded: unmark old dates to free space. Marking a date twice is idempotent, and future dates are rejected. A current streak may end yesterday while today remains open. Streaks describe retained dates. Names are case sensitive. This app records habits; it does not schedule reminders.
