# Everyday apps in Robbi V2

The V2 app library contains 40 bundled apps, including the later
[creative app expansion](CREATIVE_APPS.md). The everyday expansion added
17 apps, reuses the existing free weather integration, and extends the existing
calculator. Use **App library**, search by app name, open a tool, and run it.
Enabled tools are also included in Robbi's conversation catalog. These everyday
apps need no additional Python dependencies, resident threads, local AI models
or hardware.

## Requested integrations

| App | Available operations | Setup |
| --- | --- | --- |
| Weather | City/local-date forecast, temperatures, rain, source attribution and caching | Open-Meteo by default; no API key for noncommercial use. Set a default city or pass a location. |
| Google Calendar | Upcoming events, date-range/text search, create timed appointments | Google OAuth; configurable calendar ID and timezone. No attendee invitations. |
| Gmail | Search messages, read text/attachment metadata, compose a draft | Google OAuth. Review and send drafts in Gmail; no email-send tool. |
| Google Maps | Place search and directions links; optional address geocoding and route estimates | Links need no key. Actual geocoding/routes need a key, enabled APIs and Google billing. |
| Web search | Tavily basic search, bounded snippets and clickable source links | `TAVILY_API_KEY`. Basic depth is explicit; no automatic advanced-search upgrade. |
| Calculator | Arithmetic, powers, constants, square roots, trigonometry, logarithms, rounding and basic conversions | Offline. Functions take one argument; angles use radians. A bounded parser allows no arbitrary Python. |
| Random numbers | Inclusive integer ranges, multiple results and sampling without duplicates | Offline; operating-system randomness. |

The Google apps use environment-variable names in settings; credentials stay
outside app source/config/results. Run the desktop OAuth helper described in
[Google setup](GOOGLE_APPS.md), transfer its output privately into Robbi's existing
`.env.secrets`, and restart the runtime. You can authorize read access separately
from Calendar event creation and Gmail draft composition.

For public API terms, [Open-Meteo's free endpoint](https://open-meteo.com/en/pricing)
is limited to noncommercial use and 10,000 daily calls. Tavily documents
[1,000 free credits per month](https://docs.tavily.com/documentation/api-credits),
with one credit for a basic search. Account plans can change; see
[search/Maps setup](SEARCH_MAPS_APPS.md) for current provider links and setup.
Maps links open Google Maps; they do not themselves retrieve places or route
facts. API-backed route results identify their source and exclude live traffic.

## Twelve additional apps

| App | What it does |
| --- | --- |
| World clock | Current time and timezone conversion, including repeated/missing daylight-saving times. |
| Date calculator | Date offsets, countdowns and weekday differences with optional holidays. |
| Percentage helper | Percentage amounts, changes, discounts and markups. |
| Tip splitter | Decimal bill/tip calculation and shares that sum to the rounded total. |
| Recipe portions | Scale ingredient quantities to a different portion count. |
| Unit converter | Physical quantities, temperatures, time and binary/decimal data units. |
| Text toolbox | Word/character counts, case conversion and whitespace cleanup. |
| Password generator | Secure random passwords with selectable character groups. |
| Habit tracker | Local daily completion dates and streaks. |
| Expense tracker | Local expenses and totals grouped by currency. |
| Packing lists | Trips, quantities, packing progress and remaining items. |
| Bookmarks | Save, find and remove web links and tags without fetching websites. |

[Utility documentation](UTILITY_APPS.md) specifies input formats, storage limits,
rounding and example requests. These are working apps, not catalog placeholders.
Password results can remain in runtime job/conversation history; this utility is
not a password vault. Habit tracking does not schedule reminder notifications.

## Runtime integration

All apps use schema-generated forms, the existing two-worker app budget, input
validation and per-app deadlines. Persistent tools use bounded, atomic app state.
Google, search and Maps adapters are lazy shared services; SDK facades recheck
network permission on each invocation and discard late results after cancellation
or revocation. OAuth refresh uses bounded form-encoded requests. Remote writes
are never automatically retried. If a request times out, inspect Calendar/Gmail
before repeating it because the remote operation may have completed.

The existing owner permission system applies. Allow network/state access in the
app page when requested and then issue a new request; granting permission does
not replay a previous operation. Key-free Maps links require neither network
access nor a network grant. State and grants are not changed by installing source.

The conversation catalog retains every bundled tool within its 48,000-character
budget, omitting dashboard-only schema metadata and repeated examples. Results
render escaped text, bounded tables and HTTP(S) links; web/email/calendar content
is explicitly treated as untrusted data. Existing imported apps still have the
platform's 48-app limit.

## Validation and rollout

Run the app tests from `robbi-v2` with its Python 3.11/3.12 environment:

```console
python -m pytest tests/test_app_expansion.py tests/test_google_apps.py tests/test_search_maps_apps.py tests/test_utility_apps.py tests/test_app_http.py tests/test_app_permission_store.py tests/test_app_http_api.py
```

Provider contract tests use synthetic HTTP responses; they do not send email,
create account data, or incur provider charges. Real Google authorization and
account API access require your own account setup.

The apps are included in the runtime. Follow [installation](../INSTALL.md) for a
fresh Pi, then configure providers and app permissions in the dashboard. Keep
credentials, app state and device configuration outside source control.
