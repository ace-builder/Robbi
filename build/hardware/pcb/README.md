# PCB fabrication files

[Back to the build guide](../../README.md)

Use these three matching manufacturing files together. Their SHA-256 checksums are recorded in [the release manifest](../release-files.json).

| File | Purpose |
|---|---|
| [gerber_v1.zip](gerber_v1.zip) | Board artwork, outline and plated/non-plated drill files |
| [bom_v1.csv](bom_v1.csv) | Assembly bill of materials, including supplier part numbers |
| [pickandplace_v1.csv](pickandplace_v1.csv) | Component positions, side and rotation |

The Gerber ZIP contains **four copper layers**: top, bottom and two inner layers. Use all four layers. The BOM contains 31 grouped rows covering 59 component references; the placement file contains the same 59 references, with no unmatched references in either direction.

## Schematic

| File | Purpose |
|---|---|
| [Schematic PDF](schematic_v1.pdf) | Printable, zoomable circuit diagram |
| [EasyEDA Standard schematic](schematic_v1.easyeda.json) | Editable schematic export; import it into EasyEDA Standard |
| [Protel/Altium netlist](schematic_v1.net) | Exported component and pin-to-net list for comparison |
| [Comparison record](schematic-verification.json) | Scope and results of the checks against the published manufacturing files |

The schematic uses the **IP5306** power circuit found on this PCB. Its **59 component references, values and footprints** match the BOM and placement exports. All **208 connected schematic pins** match the pad-to-pad connections traced from the published Gerber copper and plated drills; the other 20 numbered pads have no other component pad connected in that trace. The right encoder B signal reaches **U6 pin 36 / BCM16**, and the amplifier's SD_MODE pin is tied to **3V3**.

> [!IMPORTANT]
> Several test-point references on the schematic retain names from an older design. **`TP_USB_DETECT` is connected to right motor output 2, not USB detection**; the board silkscreen calls it `MR2`. `TP_I1C_SCL` is Pi pin 27. `TP_USB_VBUS1` and `TP_USB_VBUS2` are Pi pins 10 and 28, respectively. Only `TP_USB_VBUS` without a suffix is on the USB 5 V input. Follow the actual net labels and [wiring reference](../../WIRING.md#power-test-points).

The schematic's **“Battery Protection”** heading is a legacy section label; this sheet does not show a separate cell-protection IC and FET stage. It does not establish the protection fitted to a particular cell or pack. The matching checks establish documentation consistency, not an electrical safety, charging, load or thermal qualification.

## Settings from the manufactured board

These settings were recovered from the manufacturer’s returned production package. The original Gerbers inside that package match the guide’s ZIP byte for byte.

| Setting | Value |
|---|---|
| Board outline | **75 × 45 mm** |
| Material / thickness | **FR-4 / 1.6 mm** |
| Copper layers | **4** |
| Outer / inner copper | **1 oz / 0.5 oz** |
| Solder mask / silkscreen | **Green / white** |
| Surface finish | **Lead-free HASL** |
| Via treatment | Solder-mask plugged, as recorded by the manufacturer |

The manufacturing panel is **75 × 71 mm**, including two **13 mm process rails** added along the 45 mm dimension. Those rails are manufacturing aids, not part of the enclosure-sized board. Submit the supplied 75 × 45 mm outline rather than resizing it to the panel dimensions. The exact dielectric stackup is not recorded in the recovered settings.

[Machine-readable order settings](order-settings.json) · [Connector pinout and orientation](../../WIRING.md)

## Ordering sequence

1. Upload the Gerber ZIP intact to the PCB manufacturer's order tool. Check its rendered outline, layer assignment, drill alignment and preview against the board photographs.
2. Enter the settings above: four layers, 75 × 45 mm, 1.6 mm FR-4, 1 oz outer / 0.5 oz inner copper, green mask, white silkscreen and lead-free HASL. Check the manufacturer's interpretation of the via-treatment option and let it prepare the required assembly rails.
3. Request PCB assembly, then upload the matching BOM and pick-and-place file together. Both `.csv` exports are **UTF-8, tab-delimited** text. This package converts the original UTF-16 encoding without changing field values or row content. If the upload tool asks, choose UTF-8 and the tab delimiter, and review its imported part mapping.
4. Match parts by the specified manufacturer part and supplier part number. Several free-text `link` fields in the export point to unrelated products; do not let those override the actual MPN/LCSC selection.
5. Review the assembly preview, including polarized capacitors, USB-C orientation and the underside connector. Resolve substitutions against the intended footprint and electrical function before ordering.
6. Confirm the through-hole assembly scope. **U6 is the 2 × 20 female socket, placed on the bottom side and explicitly marked “HAND SOLDER BACK SIDE”.** The BOM and placement file both include it; agree whether the manufacturer supplies/fits it or whether it is supplied for hand assembly. Do not have it fitted twice or on the top side.
7. Check whether BAT, LCD, H1/H2, encoder, speaker/servo headers, test pins and switches are included in the assembly service or supplied separately. Keep optional fitted connectors electrically unused if the guide does not use them.

## Parts already on the custom board

| Reference | Component | Function |
|---|---|---|
| U7 | IP5306, C181692 | Power-management stage |
| MD1 | DRV8833PWPR, C50506 | Two-motor driver |
| AMP | MAX98357AETE+T, C910544 | I²S speaker amplifier |
| U4 | LSM6DS3TR-C, C967633 | IMU |
| BAT | B2P-VH(LF)(SN), C160315 | Two-pin VH3.96 battery header |
| U6 | 2 × 20 female socket, C2977589 | Underside Pi connection |

The parts and visible `Robbi 2026-09` layout correspond to the board in the build photographs. The [wiring reference](../../WIRING.md) provides connector pin order and orientation checked against the manufactured copper layout. Older schematic exports using the BQ25895/TPS61088 power circuit do not describe this manufactured board.
