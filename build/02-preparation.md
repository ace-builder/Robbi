# 2. Prepare tools and printed parts

![Prepare tools and printed parts](assets/banners/02-prepare.png)

[Guide home](README.md) · [← Parts](01-parts.md) · [Software →](03-software.md)

## Tools

| Tool | Used for |
| :--- | :--- |
| 3D printer, makerspace or print service | Body, head, wheels and internal mounts |
| Temperature-controlled soldering iron, stand, solder and electronics flux | Wire joints, omitted through-hole headers and heat-set inserts with an appropriate tip |
| 1.5 mm and 2.5 mm Allen keys, plus small Phillips bits | M2 and M3 socket-head screws; a long straight leg must reach the motor clip/head fasteners |
| Slim 4 mm AF nut driver, outside diameter no more than 5.5 mm | Compact M2 board-stack nuts; check actual hardware |
| Flush cutters, wire stripper, tweezers and small pliers | Harnesses, side-loaded clip nut and split button pin |
| Heat-shrink tubing and suitable heat source | Insulating individual joints |
| Hot-glue gun and adhesive suitable for the holder/plastic | Two ToF glue fillets and accessible holder retention |
| Multimeter with continuity and DC voltage ranges | Shorts, polarity and rail measurements |
| Deburring tool, small file and calipers | Support cleanup and actual printed fits |
| microSD reader and computer | Flashing the OS and copying the release |
| Tape measure, tape and a flat test area | Wheel and motion calibration |
| Optional current-limited bench supply | First power-up only with a verified input arrangement and limit |

Use a nonconductive work surface and a tray for fasteners. Ventilate soldering work. Prepare holder leads with the cell removed; do not solder directly to a cell.

## Print the matching release

> [!WARNING]
> **The 13 October 2026 enclosure iteration has not yet been physically validated.** CAD and reference slicing checks do not establish printed fit, strength or switch operation. Plan a dry assembly and expect possible adjustment or reprinting.

Start with the [individual STLs and reference 3MF projects](hardware/prints/README.md#individual-printable-parts). Use this matching chassis, both panels, new front-fastened clip, rear cover, four 11 mm spacers, three button pieces and fixed head/cover together. Do not substitute earlier clip, panel or head parts.

The original [robo_4_astra.zip](hardware/prints/robo_4_astra.zip) remains an unchanged **assembly reference**, including modelled purchased components and the original wheel/tread shapes. Do not print the entire STL as one object or use an automatic split as a finished kit. The individual October exports avoid that ambiguity for the revised enclosure pieces. Separate wheel/tread print settings still need confirmation.

Import individual files in **millimetres at 100% scale** and keep their supplied orientation. The 3MF projects are unsliced reference projects for an A1 / 0.4 mm nozzle, not printer-ready instructions. Choose the actual machine, material, plate and nozzle, inspect supports and thin walls, and slice again.

| Group | Preparation / check |
| :--- | :--- |
| Chassis and rear cover | Clean supports from motor seats, cover keys, Pi rails, USB opening and cable passages. |
| Side panels | Preserve the 0.8 mm battery-pocket skins and locating rims; check preview continuity. |
| Motor clip | Use the new front-fastened part; the solid reference profile and gentle tightening matter near thin motor walls. |
| Button rocker, keeper and pin | PETG / 0.16 mm reference; preserve the leaf, return spring, pin diameter and split fork. Clip the pin-only brim gently. |
| Head carrier and cover | Clean the hollow neck and sliding hooks; protect the four long cover posts. |
| Wheels, treads and bearings | Check D-shaft fit, concentricity, bearing fit and actual tread material separately. |

Print the **button/USB coupon and three button parts first**. The optional ToF coupon checks its local seat. These small tests do not prove real-stack switch actuation, full USB plug seating or sensor performance through the final shell. Use the [part-by-part printing table](hardware/prints/README.md) for supports, layer height, walls, infill and exceptions.

![An earlier enclosure print on the printer bed](assets/05-printed-parts.jpg)

*Earlier print layout, not the reference orientation for every current part.*

Do not use the software's `robbi-body.stl` and `robbi-head.stl` preview models as a complete print kit. Do not rescale the enclosure to fix a local tight fit: this also moves the bearing, board and screw interfaces.

## Clean and dry-fit

1. Remove supports, strings and burrs without changing mounting datums, bearing seats or flexure dimensions.
2. Test the button/coupon motion and actual USB cable overmold. Separately check the real switch and complete plug seating with the seated stack later.
3. Dry-fit the motors and new clip before the holder, electronics, head or camera. Check actual motor retention and the thin chassis floor/wall.
4. Trial-fit the **loaded holder and both panels**, check lead exits and contacts, then remove the cell before bonding the empty holder. Its central hole stays unused.
5. Dry-fit the real Pi/header/custom PCB on **four 11 mm spacers**. Check actual Pi thickness and header engagement before tightening the four outside-entry screws.
6. Seat the display on its studs and offer the service cover with no wires first. It must meet the rim without screw force or pressure on the glass/components.
7. Follow the [current assembly order](05-assembly.md): the stack enters through the GPIO side with the head off, held 4 mm outward before moving inward. Keep cell and USB absent during the first complete fit.

## Inserts and bearings

Fit the matching **side-panel heat-set inserts while the body is empty**, as described in [assembly](05-assembly.md#side-panel-inserts). Their outside diameter and length must match the actual pockets; test on a spare boss if changing printer or material. The new clip and head use their specified captive nuts, not substitute inserts.

The bearing is **MR128ZZ, 8 × 12 × 3.5 mm**, in the circular body recess around the shaft opening. The [bearing and wheel close-ups](05-assembly.md#1-mount-the-motors-first) show the seats and D-shaped hub. Press on the outer race to seat a bearing; align shaft and hub flats and avoid hammering the gearbox. Stop if plastic splits or distorts.

**Checkpoint:** the complete unpowered enclosure fits freely; actual wires, camera ribbon, holder and board stack have clearance; the button releases; the head cover seats without pressure. Record the print revision, settings and unresolved fits in [build-record.csv](build-record.csv).
