# World clock

See current times and convert a local date and time between IANA timezones.

Use the generated forms in Robbi V2 App library, or ask Robbi using the example phrases. Its implementation travels inside this project; it requires only the declared Robbi SDK and no provider keys or network connection.

Uses the runtime IANA timezone database. Spring-forward times that never occurred are rejected; repeated fall-back times require earlier or later. The output includes a date and numeric UTC offset, avoiding timezone-abbreviation ambiguity.
