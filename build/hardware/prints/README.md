# Enclosure and print files

[Guide home](../../README.md) · [Print preparation](../../02-preparation.md) · [Assembly](../../05-assembly.md)

> [!WARNING]
> **The 13 October 2026 printed design has not yet been physically tested as a complete build.** It has nominal CAD-fit and reference slicer checks only. Fit, strength, button operation, connector access and optical clearance still need checking on actual printed parts and purchased components. The build video shows an earlier enclosure.

Use the matching files in **[2026-10-13/](2026-10-13/)** together. Do not mix earlier chassis, panels, motor clips or head parts with this revision. See [design changes and fit limits](DESIGN-2026-10-13.md).

## Individual printable parts

STLs are supplied in millimetres, already oriented for printing. Import at **100% scale**. Reference projects use a Bambu A1 / 0.4 mm nozzle; select your actual printer, nozzle, filament and build plate, then inspect and slice again. The 3MF files contain models and settings, **not ready-to-run G-code**.

Default reference settings: **PLA, 0.20 mm layers, three walls, 15% infill**. Button and clip exceptions are listed below. These are reference settings, not a physically validated print recipe.

| Part | Qty | STL | Reference 3MF | Orientation / exceptions |
| :--- | ---: | :--- | :--- | :--- |
| Main chassis | 1 | [Chassis](2026-10-13/print/Main_chassis_USB_and_button.stl) | [01](2026-10-13/slicer_projects/01_Main_chassis_REFERENCE_A1_0p4.3mf) | Supplied side down; motor axes vertical; bed-only tree/organic supports. |
| Rear electronics cover | 1 | [Rear cover](2026-10-13/print/Rear_electronics_cover.stl) | [02](2026-10-13/slicer_projects/02_Rear_cover_REFERENCE_A1_0p4.3mf) | Outside face down, Pi rails up; normal supports under raised end lips. |
| Side panels | 1 each | [Battery side](2026-10-13/print/Sidepanel_left_battery_side.stl) · [GPIO side](2026-10-13/print/Sidepanel_right_GPIO_side.stl) | [03, both panels](2026-10-13/slicer_projects/03_Both_side_panels_REFERENCE_A1_0p4.3mf) | Outside faces down; no supports. Preserve the 0.8 mm pocket skins. |
| New front-fastened motor clip | 1 | [Clip](2026-10-13/print/Motor_retaining_clip_forward_fastener.stl) | [11](2026-10-13/slicer_projects/11_Motor_retaining_clip_REFERENCE_A1_0p4.3mf) | Broad side down; no supports; four walls, 40% configured infill, **26 top and 26 bottom layers** make this small part solid. |
| 11 mm stack spacers | 4 | [One spacer](2026-10-13/print/Stack_spacer_11mm_PRINT_4.stl) | [04, four spacers](2026-10-13/slicer_projects/04_Four_stack_spacers_REFERENCE_A1_0p4.3mf) | Upright; no supports. **STL = one spacer; 3MF = four.** |
| Button rocker | 1 | [Nominal rocker](2026-10-13/print/Power_button_rocker_print.stl) | [06](2026-10-13/slicer_projects/06_Power_button_rocker_REFERENCE_A1_0p4.3mf) | **PETG, 0.16 mm**, bearing axis vertical, bed-only tree supports. |
| Button keeper and split pivot pin | 1 each | [Keeper](2026-10-13/print/Power_button_keeper_print.stl) · [Pin](2026-10-13/print/Power_button_pivot_pin_print.stl) | [07, both parts](2026-10-13/slicer_projects/07_Power_button_keeper_and_pin_REFERENCE_A1_0p4.3mf) | **PETG, 0.16 mm**, no supports; keeper flat, pin horizontal. Pin-only **3 mm outer brim, zero gap**. |
| Fixed head carrier and hollow neck | 1 | [Head carrier](2026-10-13/print/Fixed_head_carrier_with_neck.stl) | [08](2026-10-13/slicer_projects/08_Fixed_head_carrier_REFERENCE_A1_0p4.3mf) | Front bezel down, rear open upward; normal supports from bed only. |
| Head rear service cover | 1 | [Head cover](2026-10-13/print/Fixed_head_rear_service_cover.stl) | [09](2026-10-13/slicer_projects/09_Fixed_head_rear_cover_REFERENCE_A1_0p4.3mf) | Outside rear face down, posts up; no supports; reference outer-wall speed 40 mm/s. |

The button reference uses three walls, 15% configured infill and **20 top / 20 bottom layers**, making these small parts solid. Preserve the flexible rocker leaf, keeper spring and split pin. Support the fork while gently clipping off its brim. Do not enlarge the 2.4 mm button bores or sand away the 2.30 mm pin's working diameter.

### Small fit tests first

- **Button/USB coupon:** [STL](2026-10-13/print/TEST_ONLY_button_USB_fit_coupon_print.stl) · [project 05](2026-10-13/slicer_projects/05_TEST_ONLY_fit_coupon_REFERENCE_A1_0p4.3mf). Print with the three PETG button parts. The coupon checks motion, return and USB cable-overmold passage; it does **not** establish the switch gap or complete plug seating on the actual board stack.
- **Optional ToF coupon:** [STL](2026-10-13/print/TEST_ONLY_ToF_seat_coupon_PRINT.stl) · [project 10](2026-10-13/slicer_projects/10_TEST_ONLY_ToF_seat_coupon_REFERENCE_A1_0p4.3mf). Outside face down, no supports. Check the actual board, cover and solder tails; optical performance still needs testing in the robot.
- **Optional rocker alternatives:** [0.10 mm longer contact](2026-10-13/print/OPTIONAL_rocker_longer_contact_0p10_print.stl) · [0.10 mm shorter contact](2026-10-13/print/OPTIONAL_rocker_shorter_contact_0p10_print.stl). Use the nominal part first. A contact-length change requires repeating all real-stack button checks; it cannot compensate for an incorrectly seated header or wrong stack height.

Coupons and alternative rockers are test/adjustment pieces, not extra installed robot parts.

## Original assembly STL

**[robo_4_astra.zip](robo_4_astra.zip)** contains the original, unchanged **robo_4_astra.stl**. Extract it locally for an assembly reference. It includes modelled purchased electronics and contact surfaces between parts; it is **not a prepared print plate**. Do not print it as one object or interpret every disconnected surface as a printable part.

A surface comparison of the chassis, both side panels, motor clip and fixed head/rear cover agrees with the October individual exports within **0.026 mm at the sampled vertices**, after accounting for pose and tessellation. This supports the revision match; it does not establish physical fit or a complete equivalence of every purchased reference component. The new individual files are unchanged exports from the October print release, not automatic splits of the assembly STL.

### Parts identifiable in the supplied STL

The assembly also contains the original wheel hub and tread geometry. The individual October exports above cover the revised enclosure and functional print pieces; they do not include a separate wheel/tread print project.

| Original geometry | Build quantity | Handling note |
| :--- | ---: | :--- |
| Wheel centre/hub | 2 | One representative shape in the assembly; about 56 mm diameter and 20 mm depth. Preserve its D-shaped shaft opening. |
| Tread ring | 2 | One representative shape; 62 mm outer diameter and 5.8 mm axial width. Tread material and print settings still need confirmation. |
| Rear support foot/skid | 1 | Retain the underside floor-contact feature; see the [assembly close-up](../../05-assembly.md#rear-support-foot). |

The MR128ZZ bearing sits in the body recess around the shaft opening. The 62 mm tread diameter is the prototype's 6.2 cm starting wheel-diameter setting; calibrate using the finished rolling surface.

Original archive/extracted-STL hashes and the added individual-file hashes are recorded in the [hardware release manifest](../release-files.json). [Validation status](2026-10-13/validation.json) distinguishes the recorded CAD/slicer checks from physical tests still to do.
