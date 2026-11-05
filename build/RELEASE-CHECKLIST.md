# Release checklist

[Guide home](README.md)

Complete these items before labelling the release independently reproducible. This page tracks specifications and tests that remain open.

## Mechanical files

- [x] Compare the published assembly STL with the October chassis, panels, new motor clip and fixed head/cover; sampled surfaces agree within mesh export tolerance.
- [x] Include the unchanged 15 individual October STL exports and 11 unsliced reference 3MF projects; keep the original assembly STL as a reference.
- [x] Document millimetres/100% scale, reference orientation/supports and the PETG button / solid clip exceptions. These are reference slicer results, not a physically validated recipe.
- [x] Specify four M2 × 20 stack screws/four 11 mm spacers, M2 × 10 clip fastener, M2 × 6 head-cover fastener and two M3 × 12 head fasteners with corresponding nuts/washers.
- [x] Correct the written order: clip before holder/electronics; GPIO-side stack entry at 4 mm offset with head off; BAT then inward seating; head then camera.
- [x] Label the video/photographs as an earlier enclosure; remove the old adjustable-head and body-flex instructions from the current sequence.
- [ ] Physically print and dry-assemble the matching October enclosure. Check thin walls, actual motor retention, Pi/header stack height, LCD/holder clearance, button no-preload/click/release, USB seating, head hooks/posts, camera screws and ToF optics.
- [ ] Validate material strength, repeated button use, cable strain and full assembly/service routes on actual parts.
- [ ] Confirm unchanged side-panel/rear-cover/camera fasteners, insert dimensions/counts, bearing counts and any wheel retention.
- [ ] Supply separate wheel/tread print files and confirm tread material and successful print settings. The assembly reference contains the original hub and tread geometry.
- [ ] Add current-revision assembly photos after the physical build; existing photos identify components and earlier techniques.

## Electrical specification

- [x] Add a component-side connector drawing and numbered pin tables for all connectors used in this build; verify their copper connectivity in the manufactured Gerbers.
- [x] Archive the matching editable EasyEDA schematic, PDF and netlist. The exported 59 component references, footprints and values match the manufacturing BOM/placement; all 208 connected schematic pins match the traced Gerber copper. Note the legacy test-point names in the [PCB notes](hardware/pcb/README.md#schematic).
- [x] Recover manufactured board settings: 75 × 45 mm, four layers, 1.6 mm FR-4, 1 oz outer / 0.5 oz inner copper, lead-free HASL, green/white; distinguish the 75 × 71 mm assembly panel.
- [ ] Confirm exact dielectric stackup and which through-hole connectors arrived fitted; U6 is already marked for backside hand soldering.
- [x] Trace MENCODER_R pin 4 to U6 pin 36 / BCM16 in the supplied Gerbers. Use `hardware.right_encoder_b_pin: 16`; GPIO7 remains a repair option.
- [x] Trace MAX98357A SD_MODE to 3V3; installer documentation uses `max98357a,no-sdmode` to leave motor GPIO4 available.
- [ ] Confirm the installed overlay and sound output on the fresh-card acceptance run.
- [x] Document power-button startup and named PACK− / PACK+ / 5V / 3V3 test points; distinguish battery-only checks from the Pi-supplied 3.3 V rail.
- [ ] Record actual unloaded/loaded voltages, cell/protection configuration, charge settings and permitted simultaneous inputs.
- [ ] Validate the supply, duty and load limits for the 3 V motors, including startup and thermal checks.

## Software and repeatability

- [ ] Publish this reviewed source snapshot with its sealed runtime manifest under an immutable tag in ace-builder/Robbi. A separate runtime Release is optional.
- [ ] Complete a fresh Bookworm 64-bit installation and record the working package versions.
- [ ] Validate the generated config/robot.yaml, setup wizard and service on a fresh Pi, including missing peripherals, network recovery and dashboard IMU calibration.
- [ ] Complete the build record: movement, stop/fault/restart behaviour, STT, LLM, TTS and full spoken interaction.
- [ ] Have another builder follow the guide without additional private instructions.
- [x] Prepare non-commercial code, CAD/documentation/artwork licences and retain third-party notices; exclude credentials and personal runtime state. Review these proposed terms before publishing.

## Publication scope

The repository contains the reviewed software snapshot and the hardware guide. Publication does not establish physical validation of the revised printed enclosure. No `.env` files, owner data, private addresses, custom voice IDs or device-specific deployment records may enter a release.
