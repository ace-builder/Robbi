MAP & EXPLORE â€” Robbi v2

Open http://robbi.local:8082/#map

First map
1. Put Robbi upright on a level floor, away from stairs. Check front ToF and IMU
   calibration in Overview / IMU. The existing calibrated wheel dimensions and
   counts-per-revolution remain in robot Settings; navigation uses those values.
2. In Map & Explore, New map defines
   a fresh coordinate frame. The first scan position is approximately (0,0), with
   its initial forward direction along +X. This is not a compass/map-north frame.
3. Press Scan 360°. The map appears when the full scan completes. Dark cells are
   obstacles, white cells are observed free space, grey cells are unknown.
4. Press Explore. Defaults: 30 scan stops, 20 m total travel, 30 minutes maximum.
   Set smaller limits for first trials. Robbi drives to reachable frontiers, up
   to 200 cm between scans, using measured segments of at most 30 cm. It rescans
   earlier at narrow parts, new obstacles or increasing odometry uncertainty.
5. Save copy gives a map a name in the library. Checkpoints also preserve the
   latest completed scan automatically. Stop never queues a later restart.

Drive to a point
Tap a white area on the map or enter X/Y coordinates. Preview route computes an
inflated-footprint A* path without moving. Drive to point starts that route.
Unknown areas, disconnected rooms, points close to walls and openings narrower
than the configured robot footprint are rejected. During motion Robbi checks
fresh forward range, uses encoder/IMU feedback, rescans and replans. It stops
when it cannot establish a reliable location or make progress. Arrival tolerance
is about one map cell (default 5 cm; goal threshold 7.5 cm), not a guarantee of
that absolute physical accuracy. Stop navigation and the global Stop both work.

Saved maps / pickup
Load restores map geometry but deliberately leaves the current pose unknown.
Restart, detected pickup and movement outside navigation also invalidate pose.
Press Relocalize for a new scan and a search over the known map. If localization
is ambiguous, expand Help Robbi relocalize, tap near its actual position, optionally
enter approximate heading, and use the hint. Robbi still checks the scan match.
After carrying Robbi, relocalize even if the pickup detector missed a gentle lift.
No automatic driving or spinning occurs on load, reboot or placement.

Tuning
Advanced settings are stored in var/state/navigation/settings.json. All units are
centimeters/degrees; speed uses the existing motion controller's bounded command
scale. Defaults: 12 cm robot radius (include wheel/body protrusions), 4 cm clearance,
5 cm cells, 300 cm usable range, 4 cm forward sensor offset, 0 cm left / 0 degrees
bearing. Measure the footprint and sensor offset on your build. These defaults
are conservative starting values, not measurements of a new enclosure.
Resolution and sensor placement may change only with an empty map. Saved maps
retain their geometry settings. Wheel direction, track width and IMU calibration
remain shared with normal measured motion. Owner-only scripting exceptions do not affect this
navigation controller.

Implementation and limits
This is lightweight occupancy mapping and bounded scan localization, not a full
pose-graph/loop-closure SLAM stack. It needs no cloud, camera or ROS. The IMU uses
the existing gravity-aligned gyro integration and wheel/gyro fusion; acceleration
provides gravity/orientation and handling detection, not double-integrated indoor
position. Every movement consumes actual control telemetry, including a final
settled receipt. ToF rays are interpolated against pose history, with an estimated
measurement midpoint (the sensor does not expose hardware timestamps). Sparse
scan headings can optionally get up to 12 stationary follow-up samples (off by
default). Incomplete scans stop
with guidance instead of inventing observations.

The VL53L1X has a finite field of view; glass, dark/reflective materials, lighting,
low objects, wheel slip, similar rooms and long featureless corridors limit the
result. One forward sensor cannot detect cliffs. Neither the uncertainty display
nor simulator results promise centimeter absolute accuracy or complete apartment
coverage. Test a small area before extending travel limits.

Compute / storage
One worker while a job runs, no idle mapping thread; existing cached IMU/encoder/
ToF readers are reused. A rolling motion lease holds the wheels across planning,
but leaves the display and speaker usable. Stop cancels the job and motor cleanup
finishes before ownership is released. Browser draws the map and downloads cells
only on map revision changes. Numerical matching optionally uses NumPy (already
installed by the voice extra); pure Python works with bounded searches and may
request a hint sooner. Maps: max 60,000 cells, +/-600 cells per axis, 30 named maps,
5 MiB per file; atomic saves. No motion resumes automatically after a fault.

Files and integration
src/robbi/navigation/{model,grid,planning,localization,storage,hardware,service,web}.py
web/views/navigation.js, web/navigation-map.js, web/navigation.css
GET /api/navigation, /api/navigation/map?since=REVISION, /api/navigation/maps
POST /api/navigation/{scan,explore,relocalize,plan,navigate,stop,new,save,load,delete,settings}
Command requests return immediately; poll status for completion or a readable error.
Legacy /slam endpoints now forward to this service. Historical legacy map files in
var/state/maps are retained; the new map library uses var/state/navigation/maps.

## Development checks

Run `python -m pytest tests/test_navigation.py tests/test_navigation_hardware.py`.
See [architecture](development/ARCHITECTURE.md) for runtime boundaries.

See [scan rates and continuous driving](motion-and-scan.txt) for the new sensor
presets, buffered sampling, measured diagnostics, settings and validation limits.
