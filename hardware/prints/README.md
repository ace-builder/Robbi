# Enclosure model

[Guide home](../../README.md) · [Print preparation](../../02-preparation.md)

Download **[robo_4_astra.zip](robo_4_astra.zip)**, then extract **robo_4_astra.stl** on your computer before importing it into your slicer or CAD program. The ZIP contains the unchanged original assembly reference. Checksums for the archive and extracted STL are recorded in the [release manifest](../release-files.json).

The model is distributed as a ZIP so it fits GitHub’s browser-upload limit. Keep this inner ZIP compressed when uploading the guide to GitHub; builders extract it after downloading.

## Parts identifiable in the supplied STL

The mesh has been inspected as separate connected surfaces. It contains purchased electronics as well as these identifiable enclosure/wheel shapes:

| Part | Build quantity | Geometry / handling note |
|---|---:|---|
| Main body | 1 | Contains the motor seats and wire passages; check the camera/speaker openings before closing |
| Body closing/back section | 1 | Keep separate while mounting the Pi and joining the wired PCB |
| Body side panels | 2 | Two panel shapes are present; each has an approximately 108.4 × 81 × 7 mm axis-aligned bounding box in the supplied model |
| Head front/shell | 1 | Approximately 65 × 50 × 29 mm bounding box |
| Head rear/support piece | 1 | Approximately 65 × 40 × 26.25 mm bounding box, including its projecting mounting features |
| Wheel centre/hub | 2 | One representative shape is present; approximately 56 mm diameter and 20 mm overall depth |
| Tread ring | 2 | One representative shape is present; **62 mm outer diameter**, 5.8 mm axial width |
| Rear support foot | 1 | Keep this rounded floor-contact feature in the build; the [assembly close-up](../../05-assembly.md#rear-support-foot) shows its underside position |

These measurements are geometry checks, not printer clearances or a tested print recipe. The main body and closing section have open mesh boundaries when isolated; inspect their complete geometry and slicer preview rather than treating an automatic “split to parts” operation as a finished print kit. Small detached surfaces and modelled electronic components are not extra printed parts.

The 62 mm tread diameter agrees with the prototype's 6.2 cm starting wheel-diameter setting. Calibrate using the actual finished rolling surface after printing.

The wheel hub uses a **D-shaped opening** to match the motor shaft. Preserve the flat when cleaning the print; do not drill the opening into a round hole to make it fit. The MR128ZZ bearing seats in the body's circular recess around that shaft opening.

The remaining print information is **material by part, orientation, supports, wall count, infill and layer height**. A slicer project can provide these without needing a replacement STL.

> [!IMPORTANT]
> This is **not a ready-to-print plate**. The assembly includes modelled purchased components as well as printed parts. Do not print the entire file as one object or treat every disconnected surface as a printable part.

## Prepare the printable parts

Export each printable body separately with a name and quantity: main body, panels, head/bezel, bracket, mounts, wheels and tread parts. Confirm millimetres against a known component before slicing; STL does not encode units.

Add the matching slicer project or specify material, orientation, supports, layer height, walls and infill. Confirm the tread material and the insert, bearing and fastener dimensions at each location. The [release checklist](../../RELEASE-CHECKLIST.md) separates the existing geometry from the print settings and physical-fit checks still to confirm.

## Back-panel revision

In the updated design, the Pi mounting screws enter from behind, through the **outside of the back panel**. Check that the exported parts match this arrangement. Follow the [assembly sequence](../../05-assembly.md) for motor installation, cable routing and final fitting.
