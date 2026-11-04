# Release checklist

[Guide home](README.md)

Complete these items before labelling the release independently reproducible. This page tracks specifications and tests that remain open.

## Mechanical files

- [x] Inspect the supplied STL and identify the main enclosure pieces, two side panels, head pieces, 56 mm wheel centre and 62 mm tread ring.
- [ ] Prepare a tested print plate with the updated external-access Pi mounting screws; resolve the main shell/closing-section open mesh boundaries before calling the extracts print-ready.
- [ ] Confirm units, materials, orientation, supports, layer height, walls and infill; include a slicer project.
- [ ] Specify M2 screw lengths/counts, M3 × 14 counts, insert outside dimensions/counts, spacers and MR128ZZ bearing locations/counts.
- [x] Illustrate the bearing seat, D-shaped shaft/hub fit, left-angle microphone adapter, camera screw mounting and ribbon route, head retaining bracket/inside nuts, and rear support foot using the supplied assembly photographs.
- [ ] Confirm the complete fastener dimensions and any additional wheel retention used on the released build; keep the documented back-panel/camera-ribbon clearances in the print-fit checks.

## Electrical specification

- [x] Add a component-side connector drawing and numbered pin tables for all connectors used in this build; verify their copper connectivity in the manufactured Gerbers.
- [ ] Archive a fully matching editable schematic: the available schematic PDFs retain the older power circuit even though the PCB exports contain the newer IP5306 stage.
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

This complete repository package is prepared locally for owner review. Nothing has been uploaded as part of package preparation; the owner controls publication. No `.env` files, owner data, private addresses, custom voice IDs or device-specific deployment records may enter a release.
