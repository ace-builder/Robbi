# Shared hardware orchestration

Design recorded before implementation on 2026-10-11. This extends v2 only.

## Problem and existing behavior

The Pi profile already enables dual ToF at 10 Hz and IMU at 50 Hz. One worker per
service owns sampling. Fresh front ToF protects forward motion and feeds mapping;
the IMU provides acceleration, angular velocity and relative orientation. IMU yaw
is not a magnetic compass and is not added as an uncalibrated safety interlock.

App diagnostics currently pass through a slower status cache. Output locks only
serialize individual writes: they do not decide which app, conversation, timer,
mapping operation or ambient behavior owns a device. Running motion also needs
to observe app cancellation after launch. Cleanup from an old owner must never
stop a newer owner's sound or motion.

## Resource model and policy

* ToF, IMU and camera are shared inputs. Apps read bounded copies of the existing
  service's latest data, including freshness and missing-device metadata. They
  never open another bus/device, start another sampler or reconfigure a sensor.
* Motion, audio and display are exclusive output resources managed by one small
  runtime coordinator. Enforcement is at device entry points, covering apps,
  existing actions, direct HTTP controls, mapping and background presentation.
* A unique intent identifies an app invocation or robot activity. It carries a
  trusted source priority, a deadline and cancellation. Child executor work
  inherits that intent. Apps cannot choose their own elevated priority.
* Background presentation has priority 10, autonomy/mapping 20, apps 50,
  interactive robot work 60 and explicit manual controls 80. Stop bypasses
  normal acquisition and revokes all intents immediately.
* Equal/lower-priority conflicts return busy promptly. Higher priority requests
  cooperative preemption and waits only for a short bounded handover. No hidden
  drive-command backlog, automatic action retry or resume of cancelled work.
* Ownership is retained until real hardware work exits, including motor braking,
  audio subprocess reaping and a blocked display write. Expiry cancels an active
  lease; it never frees an in-flight device for another owner.
* Static app output can retain an idle display lease for a bounded hold period.
  Equal/higher priority can replace an idle presentation; background faces cannot.
  Expired idle holds are reclaimed on access, without a timer thread.
* Apps can reserve several outputs atomically for a short scene. Either all are
  acquired or none are. Nested operations reuse the same owner. An active owner
  may add free outputs immediately, but cannot wait or preempt another owner while
  retaining outputs. A conflicting expansion fails atomically, preventing
  hold-and-wait. Idle display holds allow subsequent sound or movement. Do
  network/model work before reserving outputs.

## API and implementation boundaries

`robbi.orchestration` contains a hardware-independent coordinator, intent scope
and lease type. It holds metadata/events under a short lock and invokes no
driver callbacks under that lock. There is no worker per app/resource, scheduler
thread or unbounded wait/history list. Existing driver workers observe combined
lease/caller cancellation.

The supported app SDK grows explicit ToF/IMU snapshots, bounded camera images,
asset discovery, named sound/face/video output, generated image/text display and
bounded movement. Each requires a declared capability. Output APIs return real
admission/completion/unavailable information rather than echoing a requested
action as proof of success. Multi-output sessions reuse the same app intent.
Missing hardware remains an isolated error or unavailable snapshot.

Runtime composition injects the same coordinator into media and motion,
including reconstructed media after recovery. Guarded executor submissions copy
intent context. Main-loop/background work uses explicit lower-priority scopes.
Owner-scoped cleanup replaces global audio/motion stops except operator Stop
and shutdown. Current ownership and contention appear in diagnostics.

Bundled apps remain trusted in-process extensions. Imported apps use Linux
isolation and request supported operations through this API. Software cancellation cannot forcibly terminate a blocked native
call or replace physical motor power cutoff.

## Acceptance

Tests cover sensor freshness without device I/O, missing devices, IMU enable
after startup, capability validation, atomic acquisition and bounded contention,
priority preemption, no ownership transfer during blocked I/O, cancellation while
moving, scoped cleanup, async context propagation, and app/main-loop contention.
Existing feature regressions and original-file hashes remain required. Pi timing,
sensor orientation and physical motion/audio/display still require bench testing.

## Motion and sensor details

`MotionManager.bind_orchestrator()` installs the shared coordinator. Timed,
raw-wheel, encoder and settled-turn loops observe the caller's cancellation and
the motion lease continuously. A completed broker-managed motion future contains
the result for that specific move, after braking and lease release. Mapping and
app callers consume this result rather than reading another owner's latest move.
Repeated motion by the same intent is still serialized; owning a scene does not
allow two motor workers to run together.

`stop(owner=intent)` stops only that exact invocation. Identical owner labels do
not confer ownership. SLAM gives each scan, exploration and localization worker a
new autonomy intent, so preempted mapping cannot reacquire motion or stop a later
manual command during cleanup. Parameterless `stop()` remains the explicit
global operator/shutdown path. A `drive` command containing `stop` or zero speed
only stops its own intent when orchestration is enabled.

IMU configuration can now enable its sampler after startup without restarting
Robbi. Enabling repeatedly does not create duplicate workers. Sensor snapshots
are copies; app modifications cannot change the sampler's published quaternion
or orientation. The existing ToF freshness guard remains mandatory for protected
forward motion regardless of the requesting app or source priority.
