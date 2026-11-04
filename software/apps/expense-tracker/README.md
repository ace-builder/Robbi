# Expense tracker

Keep a small local expense log and total spending separately for each currency.

Use the generated forms in Robbi V2 App library, or ask Robbi using the example phrases. Its implementation travels inside this project; it requires only the declared Robbi SDK and no provider keys or network connection.

Approve the state capability on first use, then repeat the command. Records live in this app's state.json under the configured runtime data directory and survive restarts.

At most 120 entries; storage may fill earlier for long Unicode descriptions. Amounts always have two decimal places, using half-up rounding. Each currency label has an independent total; there are no exchange rates, bank connections, refunds, tax calculations or accounting guarantees. Delete uses an exact returned ID. Date filters include both endpoints.
