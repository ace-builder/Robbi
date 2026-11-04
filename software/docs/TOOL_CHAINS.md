# Sequential conversation tools

One user turn can now collect evidence from several installed tools. After every
receipt, the model sees the original request, earlier call arguments and bounded
result excerpts, and chooses another call, an answer, a clarification or a stop.
The decision loop is in `services/tool_chain.py`; the controller adapter is in
`engine/domains/tool_chain.py`. Hardware, app workers, permissions, presentation
and background delivery continue to use their existing services.

## Decisions sent by the model

Send **one** tool at a time using its actual installed schema:

```json
{"speak":"Let me check your inbox.","app_tool":{"app":"gmail","tool":"search","arguments":{"query":"is:unread newer_than:1d","limit":5}}}
{"app_tool":{"app":"gmail","tool":"read","arguments":{"message_id":"id-from-search"}}}
{"app_tool":{"app":"google-calendar","tool":"events","arguments":{}}}
{"decision":"answer","speak":"Amazon says your parcel was delivered. No upcoming appointments were returned. Is it a gift for me?"}
```

Each line above is a **separate model decision after the preceding receipt**, not
an executable batch. Gmail and Google Calendar are installed apps; a generic
Messages app is not currently bundled. Never substitute a nonexistent Messages
source or claim it has no messages. Request access/configuration or explain the
missing source instead. The email claim is attributed to Amazon; it is not proof
that Robbi independently verified a delivery.

Other supported decisions:

```json
{"capability_search":{"query":"read recent email and appointments"}}
{"memory_search":{"query":"preferred news topics"}}
{"web_search":{"query":"today's space exploration headlines"}}
{"decision":"clarify","speak":"Which date range should I check?"}
{"decision":"stop"}
```

Existing `{"speak":"..."}` answers and single `app_tool` calls remain compatible;
`decision` is optional for ordinary answers. A terminal decision cannot contain a
tool. A `web_search` object containing only citations is not another search.
Capability discovery counts toward the limit and provides exact runtime schemas.

Weather location confirmations use the same conversation's actual candidates.
A clear reply such as “Zurich city” resumes the forecast with returned coordinates
and timezone, preserving the original day, without another planning-model call.
An exact named city is preferred over its administrative region/districts;
genuinely ambiguous cities still need clarification. Read-only receipts remain
available for the existing ten-minute conversation TTL after their worker expires,
but Stop, source isolation, app disabling and permission revocation still apply.

## Limits and authority

`ChainLimits` defaults to six decisions that request tools (including discovery,
blocked attempts and duplicate reuse), 120 seconds for the chain, 30 seconds per
tool, 90 seconds for image generation, and 20 seconds per continuation model call.
All calls are also limited by the enclosing task deadline and app timeout. The
runner never starts an extra call at the limit; the last allowed receipt still
gets a bounded final-answer opportunity. Existing bounded worker pools are reused.

The initial user-directed app call may perform an approved effect, as before.
Schema discovery alone does not consume that initial-action opportunity. After
an actual tool attempt, **new continuation app calls must declare `read_only:
true`** and run through `AppHost.call_readonly`. The existing composed-read SDK
prohibits robot outputs, state writes, nested app calls and remote write methods.
New writes or movement after external evidence require a new direct user request.
The loop does not expand the privileges of Hub apps or replace their sandbox.

All calls retain source checks and owner permissions. Gmail and Calendar service
calls check both network and account consent at the SDK boundary. Revoking access
also prevents reuse of a cached receipt. Unsupported, disabled, denied and failing
sources are recorded separately from a successfully queried empty source.

Exact repeated tool name/argument pairs within a chain reuse the existing receipt;
they never dispatch twice, even after a failure or uncertain completion. This is
per-request deduplication, not a promise of exactly-once external writes across
crashes or different requests. Late/uncertain effects must be checked at their
source before the user explicitly retries them. Restart never resumes a chain.

Stop and superseding user turns cancel ordinary conversational chains. Explicit
background jobs retain their existing longer lifetime, but still honour Stop,
owner cancellation and deadlines. Blocking providers may finish after a timeout;
their late outputs cannot cause another call or a spoken answer. Cancellation
retains available receipts and suppresses final presentation.

Email, web pages, tool descriptions and result payloads are untrusted evidence.
They cannot grant permissions or change the user's request. Prompts explicitly
require attribution and honest partial results. Structural runtime restrictions
prevent a result from introducing writes or hidden actuator actions; source
interpretation and natural-language summarisation still depend on the model.

## Runtime result format (version 1)

The runtime returns presentation plus a trace, not another executable tool plan:

```json
{
  "speak":"Amazon says your parcel was delivered. I could not check the calendar.",
  "tool_chain":{
    "version":1,"id":"chain-id","status":"answer","partial":true,
    "evidence_format":"bounded_excerpt",
    "limits":{"max_calls":6,"total_s":120},"elapsed_ms":1900,
    "steps":[
      {"call_id":"chain-id:1","index":1,"tool":"app_tool",
       "action":{"app_tool":{"app":"gmail","tool":"read","arguments":{"message_id":"id-from-search"}}},
       "dispatched":true,"status":"succeeded","elapsed_ms":300,
       "result":{"ok":true,"text":"Amazon reports that the parcel was delivered."}},
      {"call_id":"chain-id:2","index":2,"tool":"app_tool",
       "action":{"app_tool":{"app":"google-calendar","tool":"events","arguments":{}}},
       "dispatched":true,"status":"failed","elapsed_ms":5,
       "result":{"ok":false,"permission_required":true,"text":"Account access is required."}}
    ]
  }
}
```

- Chain `status`: `answer`, `clarify`, `stop`, `cancelled`, `deadline`, `call_limit`,
  `invalid_action`, `ambiguous_calls`, or `provider_error`.
- Step `status`: `succeeded`, `failed`, `blocked`, `unknown`, or `reused`.
  `succeeded` requires an explicit successful receipt. `unknown` means completion
  was not confirmed. `dispatched` means the runtime invoked the tool adapter;
  the SDK may still reject it before backend I/O. It never proves an effect.
- Reused steps have `dispatched:false` and `reused_from` referencing the original
  call ID. Read their `result.ok` too: a reused failure remains a failure.
- `partial:true` means at least one receipt was unsuccessful/uncertain or the loop
  did not reach a normal terminal decision. It is not a completeness assertion
  about the real world or every source the user might have intended.
- Excerpts preserve all call IDs. Strings, lists and nested evidence are bounded
  for Pi memory and model context. Large records may use `action_excerpt`,
  `arguments_complete:false` and `details_omitted:true`. Never replay excerpts or
  interpret missing detail as an empty result. Full admitted arguments and raw
  receipts remain in the normal provider/app records, subject to their limits.
- `app_result` remains the last executed app's legacy metadata for dashboard
  compatibility; use `tool_chain.steps` for the whole request. A blocked-only
  chain also supplies the legacy failure metadata.
- Cancellation returns `{"cancelled":true,"tool_chain":{...}}`, without speech.
  A silent stop has `decision:"stop"`. Clarification retains `decision:"clarify"`.

## Speech and image ownership

Only the initial acknowledgement and final presentation are spoken; intermediate
tool decisions do not dispatch their speech or robot effects. The existing
background lane may generate one short model-authored progress update after five
seconds. Acknowledgements and progress are cancelled when a result is ready, so
obsolete waiting messages do not delay it.

Generated images retain `display_result:{"artifact_id":"...","hold_s":10}` for
idle foreground delivery, even if another tool runs afterwards. The runtime shows
the image before speaking and protects its hold from speaking/thinking faces.
With background work disabled, the same preview-first flow shows the saved image
after the final model decision; its hold does not run out during tool latency.
That presentation call is separately bounded to ten seconds and the remaining
chain time, and its actual outcome is retained in `display_receipt`. It is a
runtime presentation step, not an additional model-selected tool decision.
Explicit preview-only requests stay previews. Existing permission, mute, output
ownership and cancellation checks still apply at delivery.

## Dataset consumers and tests

Keep the original user turn, each model decision, each actual receipt and the
terminal trace together. Never turn `dispatched`, a progress acknowledgement or a
reused/unknown receipt into a successful-action label. Preserve source attribution,
denials, failures and cancellation. Exclude truncated excerpts from exact replay
targets; use the underlying full records when available. These traces may contain
private mail or memories and are runtime data, never public repository assets.

`tests/test_tool_chain.py` exercises real installed app schemas and the SDK with
offline provider doubles: multi-step retrieval, denials/failures, missing sources,
duplicate effects, injection boundaries, cancellation, deadlines, call limits and
acknowledgement races. Existing conversation, background, image presentation and
app SDK suites cover compatibility. These tests do not contact live Google or LLM
services and do not establish real-device audio timing.
