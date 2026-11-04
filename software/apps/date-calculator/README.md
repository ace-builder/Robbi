# Dates and countdowns

Shift dates and count calendar or working days, with optional holiday exclusions.

Use the generated forms in Robbi V2 App library, or ask Robbi using the example phrases. Its implementation travels inside this project; it requires only the declared Robbi SDK and no provider keys or network connection.

Gregorian calendar arithmetic; no automatic public-holiday lookup. Business days are Monday through Friday, excluding supplied unique holidays. Ranges include the earlier date and exclude the later date; reversing dates negates the result. Countdown uses the configured timezone, default Europe/Berlin.
