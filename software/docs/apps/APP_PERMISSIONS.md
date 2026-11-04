# Owner approval for app capabilities

Design recorded before implementation, 2026-10-11. This extends the standalone
v2 app SDK and does not alter v1 or built-in robot/manual controls.

## Policy

`apps.permissions_enabled: true` is the default, including fallback boot profiles.
An app must declare a capability and have the owner's stored approval before an
SDK operation uses it. First use records a pending request and stops that tool
invocation with a structured permission result; no worker waits for a person.
The dashboard offers Allow and Deny, plus later revocation. Grants and denials
survive restart. Denied capabilities do not repeatedly create requests.

Approval does not replay the interrupted invocation. The owner or conversation
must issue a new request. Previously completed steps are not rolled back. App
developers should check needed permissions before beginning a multi-step action.
Changing an existing grant to denied/unrequested cancels that app's current and
retained SDK output intents. Every SDK access checks current consent.
Cached image/weather service handles also recheck consent on each call and
inherit the invocation's cancellation/deadline, including when an app omits
those arguments. Provider instances remain shared across apps.

`apps.read` permits a bounded call to another app's explicitly read-only tool.
The target still needs its own owner grants and remains responsible for provider
configuration. A request for a target capability names and links to that target
app; granting the parent does not grant the child. Revoking or disabling a child
invalidates dependent running requests and retained conversation context, even
when that child's read has already finished. A later grant does not resurrect
those old receipts. It cannot retract a result already delivered to the owner.

`robot.microphone` permits explicit clap timing capture for Rhythm Relay, for
one to eight seconds through the existing live microphone stream. It does not
enable the microphone service, change the persistent wake setting, store audio,
or send audio for transcription. Stop and revocation cancel the collector.

Decisions are per app ID and capability. Updates keep existing decisions and new
capabilities require new approval. Removing a project clears its decisions.
Bundled apps receive no exemption. Imported app enablement remains separate.
The local grant file is bounded and atomically written; unreadable/corrupt state
grants nothing. Steady-state checks use memory and add no worker or polling loop.

## Owner boundary and development

Only the parent owner dashboard has permission-decision controls. The custom app
iframe bridge and conversation/tool APIs cannot grant permissions. Dashboard
sign-in is independently controlled by `server.auth_enabled`; the fresh installer
sets it to `true`, while existing explicit settings are preserved.
With sign-in off, dashboard clients on the trusted network can make manual
owner decisions. This does not grant an app any capability automatically.
When sign-in is enabled, the existing `ROBBI_WEB_TOKEN` identifies the owner;
every HTTP route requires the password. Cross-origin write protection continues
to apply in both modes. Installing, scanning, enabling and removing app projects
use this same owner boundary.

Use **Settings → Dashboard access** to require a password later. The switch
persists to the selected configuration and applies immediately without restarting
Robbi. The form can set/change `ROBBI_WEB_TOKEN` in `.env.secrets` without a restart;
the dashboard never returns the stored password. Username is `robbi`, password is the token. Setting
`server.auth_enabled` in the full YAML configuration applies after restart.
Merely having a saved token does not enable sign-in. App approval grants, denials
and the independent `apps.permissions_enabled` flag are unchanged by this switch.

`GET /api/dashboard-access` returns `auth_enabled`, `password_configured` and
`restart_required: false`. POST accepts only a Boolean `auth_enabled`. A failed
save or missing password leaves the live policy unchanged. Keep password sign-in
enabled when access is not restricted to a trusted network; plain HTTP does not
encrypt Basic credentials, so use an SSH tunnel or TLS there.

`apps.permissions_enabled: false` gives enabled apps every supported SDK
capability, including undeclared ones. It changes no stored decisions and takes
effect after restarting with the changed robot profile. Hardware configuration,
motion bounds, Stop and orchestration remain enforced. The dashboard indicates
that permission checks are disabled.

Imported Python projects run in Linux OS isolation; the permission system
governs the supported SDK operations brokered to the parent. Bundled projects
retain the trusted in-process path. Robot status exposes device health,
not raw sensor measurements that could bypass specific data permissions.

## Interfaces

App catalog/detail entries include `access`: `enforced`, `permissions` (items
with `id`, `label`, `description`, `status`), `pending` (capability IDs), `error`.
Statuses are `unrequested`, `pending`, `granted`, `denied`, or `bypassed`.
`GET /api/apps/<id>/permissions` returns `{access, owner_can_decide, owner_hint}`.
`PATCH /api/apps/<id>/permissions` accepts `{decisions: {capability: status}}`
with decisions `granted`, `denied` or `unrequested`, returning the same shape.
Only declared capabilities are editable. Development mode lists all capabilities.

Permission failures retain a job result: `ok: false`, `permission_required: true`,
`state: permission_required|permission_denied`, `app`, `permissions`, `text`.
They do not mark healthy apps broken. No approval method is exposed in the SDK.

## Owner workflow

1. Open **App library**, then the app. A permission request appears in its
   **App permissions** panel after the first attempted access. You can also
   approve declared capabilities in advance.
2. Choose **Allow** or **Deny** for each capability. Run the tool again when
   ready. Previously granted capabilities are not requested again.
3. Use **Revoke** to withdraw a grant. Current SDK work from that app is
   cancelled; other apps and manual controls retain their own output ownership.

The file `var/app-runtime/permissions.json` holds decisions (or the equivalent
path under `--data-dir`). Back it up with robot state. Corrupt files grant no
access and are preserved until an explicit owner decision replaces them with a
valid store. That recovery restores only newly chosen decisions, not old grants.
Permission checks read memory; disk writes occur only when a request or decision
changes. Update manifests to declare newly needed permissions.

To disable checks for development, add this to the active robot configuration,
or change it under **Settings → Full configuration file**, then restart:

```yaml
apps:
  permissions_enabled: false
```

Set it back to `true` and restart to restore the saved approval policy. Saving
the full profile uses the same owner boundary because it contains this bypass
switch. The ordinary live component-settings API cannot change this flag.

Current setup and isolation contract: [security and review notes](../security-and-review.txt). Imported apps cannot bypass OS isolation with the SDK development permission switch.
