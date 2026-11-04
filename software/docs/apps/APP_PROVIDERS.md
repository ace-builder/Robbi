# App provider configuration

Image and weather adapters live in `src/robbi/app_services`. They use the Python standard-library HTTP client; provider SDKs, model weights and inference runtimes are not needed on the Pi. Services are created lazily by the host. Importing these modules makes no network request.

The 2026-10-23 [everyday app expansion](EVERYDAY_APPS.md) also adds Google
Calendar/Gmail, Maps and Tavily search services in this directory. See
[Google account setup](GOOGLE_APPS.md) and [search/Maps setup](SEARCH_MAPS_APPS.md).
That expansion includes a successful live Open-Meteo forecast check; Google
and Tavily account integrations were verified with synthetic responses.

These contracts were checked against the official documentation on 2026-10-11. Models, account access and free tiers can change. No real provider account or API was called during development; regression tests use synthetic responses. Endpoint and model fields remain configurable.

## Shared behavior

- App execution has a host deadline. Services accept `cancellation=callable` and an absolute `time.monotonic()` deadline, check both before requests and while consuming responses, and propagate cancellation without a result.
- HTTPS is the default. Set `allow_http: true` only when configuring an HTTP endpoint, such as a local generation server. URLs cannot include embedded usernames/passwords. Endpoints are owner configuration, not model-supplied tool arguments.
- Requests and responses are bounded. HTTP request JSON is at most 256 KiB; image response JSON at most 20 MiB; image bytes at most 12 MiB; weather responses at most 1 MiB. Gzip/deflate expansion is bounded as well.
- No request is automatically retried. In particular, a failed or interrupted paid image submission is never submitted a second time implicitly.
- Redirects preserve authentication only on the same origin. Image downloads never receive the generation API key. BFL polling is permitted on BFL's regional API hosts; other polling URLs must keep the configured API origin.
- Error messages omit URLs, signed download links, provider response bodies and credentials. The bundled app dashboards accept only `api_key_env`, the environment variable name; set the actual value in Robbi's deployment environment or `.env.secrets`. The lower-level service also accepts `api_key` for injected adapters, but the bundled dashboards deliberately never save a raw key.
- Cancellation cannot forcibly interrupt a native DNS lookup or an already-blocked socket read. Socket timeouts still apply. For asynchronous Replicate jobs, `Cancel-After` also bounds remote work. Other providers may continue a billed generation after Robbi stops waiting; no cancellation/refund is promised.

## Image generation

`ImageService.generate(config, prompt, cancellation=..., deadline=...)` returns `GeneratedImage(data, media_type, provider, model, width, height)`. It validates PNG/JPEG/WebP containers and pixel dimensions before the host decodes and fits the image to Robbi's screen. It does not save files, access the display, or include image bytes in conversation/tool results. The host owns those operations.

Common configuration:

```yaml
provider: openai
model: gpt-image-1-mini
api_key_env: OPENAI_API_KEY
endpoint: ""                  # Empty uses the provider default.
timeout_s: 90                 # 1–180 seconds, also limited by the host deadline.
max_bytes: 12582912            # May be reduced, never increased above 12 MiB.
max_pixels: 16000000           # May be reduced, never increased above 16 MP.
request_options: {}            # Extra JSON fields specific to this provider.
```

Provider | Configuration | Wire contract
--- | --- | ---
`openai` | Model required; `size: 1024x1024`, `quality: low`; default key variable `OPENAI_API_KEY` | POST `https://api.openai.com/v1/images/generations`; one image; default JPEG output; decode `data[0].b64_json`.
`google` | Model required; `aspect_ratio: "1:1"`; key variable `GEMINI_API_KEY` | POST `https://generativelanguage.googleapis.com/v1beta/interactions`; image content from `model_output` steps. Default requested output is 1K JPEG.
`google_generate_content` | Model required; key variable `GEMINI_API_KEY` | Google's legacy `models/{model}:generateContent` contract; decode non-thought image parts from `candidates`.
`bfl` (or `flux`) | Model is the endpoint name, e.g. `flux-2-pro`; `width`, `height` default 1024 and are limited to 256–2048; key variable `BFL_API_KEY` | POST to `https://api.bfl.ai/v1/{model}`, poll the returned `polling_url`, download `result.sample`.
`replicate` | Model is `owner/model`; key variable `REPLICATE_API_TOKEN`; `request_options` becomes the model's `input` fields | POST `https://api.replicate.com/v1/models/{owner}/{model}/predictions`, poll `urls.get`, download the first output image.
`generic` | Explicit endpoint plus request/response mapping; optional key | Synchronous custom JSON API or raw PNG/JPEG/WebP response.

The OpenAI adapter always sets `n: 1` and does not send the obsolete `response_format` field to GPT Image models. A configured model must support image generation; access depends on the account. See [OpenAI's image API reference](https://developers.openai.com/api/reference/resources/images/methods/generate).

Google's current adapter uses Interactions, while `google_generate_content` is an explicit compatibility option. See [Google image generation](https://ai.google.dev/gemini-api/docs/image-generation), [the Interactions migration response format](https://ai.google.dev/gemini-api/docs/migrate-to-interactions), and [legacy Generate Content images](https://ai.google.dev/gemini-api/docs/generate-content/image-generation). Set the model supported by your account; changing providers does not automatically translate model names.

BFL results are asynchronous and signed delivery URLs expire, so the service downloads the finished image immediately. `poll_interval_s` defaults to 1 second and is configurable from 0.2–10 seconds. See [BFL's generation and polling guide](https://docs.bfl.ai/quick_start/generating_images).

Replicate models have different input schemas. Put the chosen model's options in `request_options`, for example `aspect_ratio` or `num_outputs: 1` when supported. The standard adapter targets the official model endpoint; a different `endpoint` can point at a compatible service. Arbitrary version-based prediction payloads can use `generic` only if the endpoint returns its completed result synchronously; custom async/version workflows should provide a separate adapter. See [Replicate's HTTP reference](https://replicate.com/docs/reference/http).

Example custom endpoint:

```yaml
provider: generic
endpoint: http://image-server.local:9000/generate
allow_http: true
model: my-image-model
api_key_env: HOME_IMAGE_API_KEY  # Optional for a private local service.
auth_header: Authorization
auth_prefix: "Bearer "
request_template:
  model: "{model}"
  input:
    prompt: "{prompt}"
response_encoding: url        # base64, url or binary
response_path: output.image   # Dot-separated keys and numeric array positions.
```

Template substitutions replace literal `{prompt}` and `{model}` inside string values; they do not evaluate code. With `response_encoding: binary`, the HTTP response body is the image and `response_path` is unused. A compatible OpenAI-style endpoint can simply use `provider: openai` and override `endpoint`.

## Weather

`WeatherService.forecast(config, location, day="tomorrow", latitude=..., longitude=..., timezone=..., country_code=..., cancellation=..., deadline=...)` returns structured forecast data with a readable `summary`, location, local date, units, attribution and retrieval time.

```yaml
provider: open_meteo
default_location: Berlin
country_code: DE
language: en
timeout_s: 25
cache_ttl_s: 600
endpoint: ""                  # Empty uses the provider default.
geocoding_endpoint: ""        # Empty uses Open-Meteo's geocoder.
```

`day` accepts `today`, `tomorrow`, `day after tomorrow`, their German equivalents, or `YYYY-MM-DD`. Relative dates use the destination's IANA timezone, including DST. A city supplied by the caller overrides configured home coordinates and the home country filter. Geocoding returns at most five bounded choices with valid coordinates; ambiguous names require a choice instead of fetching a forecast for a guessed city. Pass the selected latitude, longitude and timezone to the next call. A request-level country filter can reduce ambiguity; the configured country applies to the default city only.

You can bypass geocoding with `latitude`, `longitude` and `timezone` together in the request or default configuration. Request coordinates require their own complete pair and timezone; a missing axis or timezone is never taken from the home configuration, and the home city name is not attached to remote coordinates. Coordinates alone are insufficient because “tomorrow” must have a local meaning. The Pi's OS supplies timezone data; Windows installs the small `tzdata` package through the project dependencies.

Provider | Setup | Forecast behavior
--- | --- | ---
`open_meteo` | No key for the public noncommercial endpoint; optional `api_key_env` for a configured subscription endpoint | Daily forecast values, explicit local dates, Celsius/mm/km/h. Available dates depend on the provider's forecast horizon.
`met_no` | No key; required `user_agent` including your real contact email or website, e.g. `Robbi/2 person@example.com` | MET Norway Locationforecast compact; daily summary built from non-overlapping forecast periods. Temperature values are sample ranges, not guaranteed daily extrema.
`openweather` | `api_key_env: OPENWEATHER_API_KEY` | Uses the free-tier 5-day/3-hour endpoint, not One Call. Sample ranges and partial-day coverage are explicit.
`generic` | Explicit normalized endpoint, optional Bearer key | GET with `latitude`, `longitude`, `location`, `date`, `timezone`; normalized JSON response below.

Open-Meteo's public free API permits noncommercial use, including home automation, and requires attribution under CC BY 4.0. Paid/commercial use requires the appropriate endpoint/account. Robbi preserves provider and geocoding attribution in each successful result. See [forecast documentation](https://open-meteo.com/en/docs), [geocoding documentation](https://open-meteo.com/en/docs/geocoding-api) and [current terms](https://open-meteo.com/en/terms).

MET Norway requires identifiable requests, caching, attribution and coordinates with no more than four decimals. Robbi sends an explicit configured User-Agent, rounds request coordinates to four decimals, respects `Expires`, and revalidates with `If-Modified-Since` when available. See [MET's terms](https://docs.api.met.no/doc/TermsOfService.html) and [forecast period semantics](https://docs.api.met.no/doc/ForecastJSON.html).

OpenWeather's listed free access includes the 5-day/3-hour forecast and requires a key. Check the account's current quota. Its precipitation amounts cover the preceding three hours, so a period ending at the next local midnight belongs to the requested day's precipitation total; that next-day temperature is excluded. See [OpenWeather's forecast contract](https://openweathermap.org/api/forecast5) and [free access plans](https://openweathermap.org/price).

MET/OpenWeather precipitation summaries count only complete, non-overlapping periods within the selected local day. Coverage is measured against the actual local-midnight boundaries, including 23/25-hour DST days; gaps and missing precipitation remain explicitly partial. Invalid probabilities, negative precipitation/wind and missing measurements are unavailable rather than invented zeros. OpenWeather's documented omission of a rain/snow phenomenon is treated as zero, while an explicitly invalid supplied value stays unknown. Unit conversions and period sums happen before rounding.

The in-memory cache is limited to 16 responses and 2 MiB. Forecasts are semantically validated before replacing cached data, so a malformed refresh cannot destroy the last usable forecast. A changed HTTP 200 response clears an obsolete Last-Modified validator when it supplies none. Failed requests can return cached weather up to six hours old, explicitly marked `stale: true` with its last successful retrieval/revalidation time. The summary states that refresh failed and separately flags partial coverage, without assuming every refresh error means the network is offline. A cancelled or expired app request never silently becomes a cache result. No background weather polling runs while the app is unused. Restarting Robbi clears this cache.

Custom weather endpoint response:

```json
{
  "date": "2026-10-12",
  "condition": "partly cloudy",
  "temperature_min": 9,
  "temperature_max": 16,
  "precipitation": 1.2,
  "precipitation_probability": 35,
  "wind_speed_max": 12,
  "partial_day": false,
  "units": {"temperature": "C", "precipitation": "mm", "wind_speed": "km/h"}
}
```

The requested date and units must match; unavailable numeric measurements may be `null`. Set `attribution_name` and `attribution_url` for the configured service. Generic endpoints must normalize their provider's measurements server-side; Robbi never guesses units from values.

## Extending adapters

Apps consume the services rather than importing hardware controllers. A new provider can implement a narrow request/parser branch or an independent service with the same cancellation/deadline contract. Tests inject an object with `request(method, url, **kwargs) -> HttpResponse`, so API credentials and hardware are unnecessary for development.

Provider regression tests cover compressed response limits, credentials across redirects, cancellation/deadlines, malformed and oversized images, regional polling, ambiguous locations, local-date/DST resolution, cache revalidation, offline stale results, precipitation period aggregation and explicit units. They validate adapter contracts, not live account availability or API billing.
