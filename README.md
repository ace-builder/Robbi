# Robbi

![Very early version, provided as-is. Experienced makers only. The current printed enclosure iteration has not been physically tested; CAD and reference slicer checks only.](build/assets/early-version-warning.svg)

![Robbi driving](build/assets/robbi-driving-banner-2d.gif)

A tiny Raspberry Pi robot with a face, a camera, a voice and two encoder-equipped wheels. This repository brings together the illustrated hardware build guide and the software that runs on Robbi.

## Choose your starting point

| Build the robot | Install and use the software |
| :--- | :--- |
| **[Hardware build guide](build/README.md)** | **[Software guide](software/README.md)** |
| Parts, tools, 3D prints, PCB manufacturing, wiring, assembly and calibration. | First installation, dashboard, voice providers, apps, configuration and development. |
| [Parts list](build/01-parts.md) · [Wiring](build/WIRING.md) · [PCB schematic](build/hardware/pcb/schematic_v1.pdf) | [Step-by-step setup](software/SETUP.md) · [Software documentation](software/docs/README.md) |

## From parts to a working Robbi

1. **Order and prepare:** follow the [parts list](build/01-parts.md) and [printing/tool preparation](build/02-preparation.md).
2. **Install the software:** prepare Raspberry Pi OS Bookworm Lite 64-bit, connect by SSH and follow [first setup](software/SETUP.md).
3. **Test on the bench:** verify the bare PCB, connect it to the Pi and check each peripheral using the [bench tests](build/04-bench-tests.md).
4. **Assemble:** fit the motors and new clip, holder and button, then insert the electronics stack **before attaching the fixed head**. Follow the [assembly guide](build/05-assembly.md) for the 13 October 2026 printed design.
5. **Calibrate and check:** complete [IMU and motor calibration and voice tests](build/06-calibration.md). Use [troubleshooting](build/07-troubleshooting.md) if a check fails.

The core hardware is a Raspberry Pi Zero 2 W, the custom Robbi PCB, two N20 encoder motors, a small SPI face display, USB microphone, speaker, camera and forward-facing distance sensor. Bench-test the electronics before closing the shell.

## Software installation

On the prepared Pi, as your normal Linux user:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/ace-builder/Robbi/main/software/install.sh | bash
```

The guided installer verifies a pinned source snapshot, sets up the runtime and startup service, and asks for your own dashboard credentials and provider settings. Follow the [complete setup instructions](software/SETUP.md) for requirements and first checks. A fresh-card installation and the actual hardware still need physical verification.

## Watch the assembly

[![Watch the Robbi video build guide](build/assets/build-video-thumbnail.png)](https://youtu.be/MBummcW5DIk)

The silent 4K build video shows an earlier enclosure. Its electronics tests and component close-ups remain useful; follow the current written assembly order for the revised printed parts. See [what changed](build/hardware/prints/DESIGN-2026-10-13.md).

## Known limitations

These notes distinguish the filmed prototype from the **13 October 2026 printed design**. That enclosure iteration has CAD and reference slicer checks, but **has not yet been physically printed and assembled for validation**. Check actual component fit, button operation, cable clearance and motor retention before powered use.

### Power and charging

- **Charger connection:** Plugging in a charger can briefly interrupt the supply and brown out the Raspberry Pi. **Shut Robbi down cleanly, wait for shutdown to finish, and switch power off before connecting a charger.**
- **Battery cutoff:** The protection cutoff is reported at approximately **3.1 V** in this build. For routine use and battery longevity, earlier low-battery warning and orderly shutdown would be preferable to regularly reaching the protection cutoff.
- **Access to controls:** On the filmed prototype, the charging port is inaccessible from outside and the power button requires the left panel to be open. The October printed revision adds a rear USB opening and an external button mechanism. Actual cable seating and button actuation still need physical testing; these mechanical changes do not fix the charger-connection brownout.

### Movement, sensing and audio

- **Driving accuracy:** N20 motors with encoders improve control, but they do not offer the precision of high-end smart servos such as Dynamixel units. Calibration, gearbox play and wheel slip affect repeatability.
- **Uneven surfaces:** Motor torque and traction can be insufficient on **high-pile (shag) carpet**, slopes and uneven ground. Smooth, level floors work best.
- **Position drift:** Encoder feedback measures wheel rotation, not actual movement over the floor. Position errors can accumulate over longer routes; mapping and navigation still need supervision and repeated checks.
- **Limited obstacle coverage:** The single forward-facing ToF sensor does not provide rear obstacle detection or cliff detection. Keep driving tests away from desk edges and stairs.
- **Microphone while driving:** Speech may not be picked up reliably while the motors run. Motor noise inside the shell is a suspected cause. Stopping before speaking is the more reliable approach.
- **Camera quality:** The camera is adequate for basic interaction, but image quality is modest.
- **Speaker volume:** The small speaker can be difficult to hear with background noise. A compatible 1–2 W speaker and improved mounting could help, subject to amplifier, impedance, power and space constraints.

### Mechanical design and assembly

- **DuPont connections:** The current build uses friction-fit DuPont leads. They can loosen with movement or repeated assembly; keyed, locking connectors would be a better long-term solution.
- **Camera ribbon clearance:** The flex cable has very little room and several tight bends. Assembly is awkward, and the routing needs better clearance and strain relief for vibration and repeated servicing.
- **ToF mounting:** The sensor is held with hot glue. A dedicated bracket would make alignment and servicing more repeatable.
- **Revised enclosure:** The October chassis, panels, motor clip, button mechanism and fixed head/rear cover have not been physically validated together. The head is fixed; it has no turning or tilting joint. Thin walls, tight harness clearances and the new cover hooks need particular care during a dry assembly.
- **Assembly effort:** Many screws and nuts, restricted access and tightly packed wiring make assembly tedious. The enclosure needs further work to simplify assembly and maintenance.

A future PCB revision could also explore an **on-board 18650 holder**, with suitable cell retention, electrical clearance and enclosure space. This is a proposed improvement.

## Repository layout

```text
README.md       Project overview and setup sequence
build/          Illustrated guide, parts, wiring, photos and hardware files
software/       Runtime, installer, apps, assets and software documentation
```

## Licensing and contributions

Personal non-commercial building and modification are welcome under the included terms. First-party software uses PolyForm Noncommercial 1.0.0; first-party hardware, documentation and artwork use CC BY-NC-SA 4.0 where applicable. See [licensing](software/LICENSE.md), [third-party media notices](software/docs/MEDIA.md) and [contribution guidance](software/CONTRIBUTING.md).
