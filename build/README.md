# Build Robbi

![Very early version, provided as-is. Experienced makers only. The current printed enclosure iteration has not been physically tested; CAD and reference slicer checks only.](assets/early-version-warning.svg)

![Build Robbi — a small robot, built by you](assets/banners/00-build.png)

A small Raspberry Pi robot with a face, a camera, a voice and two encoder-equipped wheels. Build the electronics on the bench, fit them into the printed body, then bring Robbi's movement and conversation to life.

## Software and first setup

The reusable runtime is in [`software/`](../software/README.md). It includes the full generic app library, generated expressions and media, voice conversations and memory, provider configuration, manual control, IMU visualization and experimental mapping. Owner data, keys, captured photos, private training workflows and the Filming dashboard are excluded.

Start with the **[complete first-install walkthrough](../software/SETUP.md)**. Once the Pi is reachable by SSH:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/ace-builder/Robbi/main/software/install.sh | bash
```

The terminal wizard handles your own API keys, model and voice choice, dashboard password, Wi-Fi and optional Telegram. Normal operation then moves to the dashboard. See [licensing](../software/LICENSE.md): personal non-commercial use and modification are welcome; commercial permission is separate.

## Watch the build

[![Watch the Robbi video build guide](assets/build-video-thumbnail.png)](https://youtu.be/MBummcW5DIk)

**11:46 · 4K · silent, with on-screen instructions.** The footage shows an earlier enclosure. Use it for component identification, electronics tests and general techniques; follow the written steps for the **13 October 2026 printed revision**. Its new motor clip, button, fixed head and stack insertion order are not demonstrated by the older footage.

[Bench tests](https://youtu.be/MBummcW5DIk?t=59) · [Motors and wheels](https://youtu.be/MBummcW5DIk?t=136) · [Head assembly](https://youtu.be/MBummcW5DIk?t=305) · [Final checks](https://youtu.be/MBummcW5DIk?t=626)

> [!IMPORTANT]
> **Printed design not yet physically validated.** The matching individual STLs, reference 3MF projects, revised fasteners and mechanical assembly order are now included. CAD fit and slicer checks do not establish real-world fit or strength. Complete the dry-fit checks before gluing or powered use. Remaining gaps include original wheel/tread print settings, unchanged fasteners, motor-cable identification and measured power/load limits. See the [print files](hardware/prints/README.md) and [release checklist](RELEASE-CHECKLIST.md).

## Start here

Follow the order below. **Finish the bench tests before mounting the electronics in the shell.**

| Step | What you will do |
| :--- | :--- |
| **[01 · Parts](01-parts.md)** | Order the matching board, modules, motors and hardware. |
| **[02 · Preparation](02-preparation.md)** | Gather tools, prepare printed parts and check the fits. |
| **[03 · Software](03-software.md)** | Flash Bookworm, run one installer command and finish in the dashboard. |
| **[04 · Bench tests](04-bench-tests.md)** | Test the battery-powered bare PCB, join it to the Pi, then check the peripherals. |
| **[05 · Assembly](05-assembly.md)** | Fit motors and clip, ToF and holder, button and board stack; attach the fixed head, then camera and panels. |
| **[06 · Calibration](06-calibration.md)** | Follow IMU calibration, check wheel movement and test STT, LLM and TTS. |
| **[Troubleshooting](07-troubleshooting.md)** | Isolate a fault without changing several things at once. |

## What you are building

| Core | Hardware |
| :--- | :--- |
| Computer | Raspberry Pi Zero 2 W with a soldered 40-pin header |
| Main board | Custom `Robbi 2026-09` PCB: power circuit, motor driver, speaker amplifier and IMU |
| Movement | Two matching 3 V / 75 RPM N20 motors with encoders |
| Face and voice | 1.69-inch SPI display, USB microphone and miniature speaker |
| Vision and distance | IMX219 camera and one forward-facing VL53L1X-family ToF module |

The second ToF connector and servo outputs are unused in this build. Use the PCB and print revisions listed in this guide together. The [revision notes](hardware/prints/DESIGN-2026-10-13.md) explain the differences from the filmed prototype and the checks still needed.

## Known limitations

Read the [current prototype limitations](../README.md#known-limitations) before ordering parts or starting assembly.

## Keep these nearby

- **[Parts list](bom.csv)** — quantities, variants and purchase links.
- **[Wiring reference](WIRING.md)** — top-view connector drawing, numbered pins, display power-pin swap and power test points.
- **[Build record](build-record.csv)** — record your revisions, measurements and test results.
- **[PCB manufacturing files and schematic](hardware/pcb/README.md)** — Gerbers, BOM, placement, schematic PDF and editable EasyEDA source. **[Enclosure model](hardware/prints/README.md)**.
- **[One-command installation](03-software.md)** — minimal terminal setup, automatic startup and one generated configuration.
- **[Release readiness](RELEASE-CHECKLIST.md)** — runtime publication and fresh-card testing status.

> [!TIP]
> Keep power disconnected while changing wiring or fitting parts. For the first powered motor checks, raise both wheels. Floor tests need a clear, level area away from edges: the forward ToF sensor does not provide cliff detection.

## Repository layout

This guide, its images and manufacturing files live together under `build/`. The reusable runtime, installer and software documentation live under [`software/`](../software/README.md). Start with the [project overview](../README.md) for the complete setup sequence.
