# 5. Assemble Robbi

![Assemble Robbi](assets/banners/05-assembly.png)

[Guide home](README.md) · [← Bench tests](04-bench-tests.md) · [Calibration →](06-calibration.md)

These steps follow the **13 October 2026 printed design**. Finish the [bench tests](04-bench-tests.md) first, and keep the [wiring reference](WIRING.md) nearby.

> [!WARNING]
> **This enclosure iteration has not yet been physically validated.** The [print files](hardware/prints/README.md) have CAD and reference slicing checks only. Complete a dry assembly with the cell out and USB disconnected before gluing or powered use. The [video](https://youtu.be/MBummcW5DIk) and photographs show an earlier build; use them for component identification and general techniques, not the revised mechanical order.

**Build order:** motors and new front-fastened clip → ToF and holder → button → prewired Pi/PCB/rear-cover stack → side insertion → fixed head and display → camera and closure.

Keep the head and **both** side panels off until the electronics stack is seated. The GPIO side means the side beside the Pi's long 40-pin header, named `Sidepanel_right_GPIO_side` in the print set.

## Before assembly

- Remove supports and burrs from the motor seats, cable passages, nut pockets, cover keys, button slot and head hooks. Preserve the thin panel skins and button flexures.
- Test the button mechanism in its coupon; try the actual USB cable overmold. Separately test the actual stack and switch later: the coupon does not reproduce their final position.
- Try the display on its four locating studs and offer up the head cover without wires. It must meet the rim without forcing the glass, flex cable or PCB.
- Finish soldering and insulate all wire joints with the cell removed. Identify each lead by signal; leave service slack. Do not postpone hot work until a cell is enclosed beside the joint.
- Set aside the [revision-specific fasteners](01-parts.md#october-2026-fasteners-and-holder-fit). The new head uses **M3 × 12**, not the prototype's M3 × 14 purchase selection.

### Side-panel inserts

Fit the matching M2 heat-set inserts into the side-panel attachment pockets while the body is empty and the cell is removed. Match outside diameter and length to the actual pockets; M2 specifies only the internal thread. Guide each insert straight with an appropriate heated tip, let the heat do the work, and allow it to cool before trying a screw. Do not use the iron to force a wrong-size insert into a thin wall. The unchanged insert sizes/counts and side-panel screw lengths still require confirmation on the actual print.

![Installing a side-panel insert on the earlier body](assets/side-panel-inserts.jpg)

## 1. Mount the motors first

**You need:** main chassis, two matching encoder motors and leads, the **new front-fastened motor clip**, one M2 × 10 socket-head screw, one M2 nut and no washer for that clip.

1. Define left and right from **Robbi's viewpoint looking forward**. Label the motor/encoder leads `L` and `R`; attach their matching plugs while accessible.
2. Clean the new clip boss's side-loading nut slot and driver passage. From the open GPIO side, use tweezers to slide the M2 nut into the front-facing boss pocket. Keep it in position while starting the screw.
3. Seat the gearboxes in the existing motor locations, with shafts aligned to the wheel openings. Keep wires clear of the rear encoder boards and rotating magnets.
4. Introduce the new clip from the GPIO side, about **6 mm rearward and 15 mm above** its final position. Lower it behind the fixed camera frame, then slide it **6 mm forward** onto its boss. The motor contact lands must sit on the motors.
5. Insert the **M2 × 10 screw downward**, without a washer. Use the long straight leg of a 1.5 mm Allen key through the open body-top/neck passage. Tighten gently until seated; do not bow the clip foot.
6. Check actual motor retention and the thin printed motor wall. The old clip's rear screw position conflicts with the revised holder and must not be reused.
7. Seat the **MR128ZZ bearing** squarely in the circular body recess at each motor-shaft opening. Align the flat on the **D-shaped shaft** with the flat in the wheel hub; press the wheel on straight without hammering the gearbox. Check wheel/body clearance. Leave the motor leads disconnected from the PCB until the wiring checks call for them.

![Motor position on the earlier body; use the revised front-fastened clip](assets/motor-seat.jpg)

![Motor lead identification and routing on the earlier build](assets/motor-leads.jpg)

| Bearing and motor shaft | Wheel hub |
| :--- | :--- |
| <img src="assets/build-details/bearing-d-shaft.jpg" alt="Bearing seated in the body's circular recess with the D-shaped motor shaft visible" width="460"> | <img src="assets/build-details/wheel-d-hub.jpg" alt="Wheel hub with a matching D-shaped opening" width="460"> |
| Keep the bearing square in its seat. | Preserve the flat; do not drill the hub round. |

**Checkpoint:** motors cannot shift under the clip, wheels turn freely, and no wire can reach an encoder magnet or shaft. Complete this before fitting the holder, head, camera module or electronics.

## 2. Fit the ToF and battery holder

1. Dry-fit the actual ToF board and cover in the forward locating outline. Keep the bottom header edge open and check that real solder tails clear the curved shell.
2. Test readings through the front window using the already bench-tested electronics outside the shell. Power down and disconnect again before applying glue.
3. Apply **two small hot-glue fillets at the outer ears**, clear of optical windows, pins and connector contacts. There are no ToF mounting screws, snap clips or rear clamp in this design.
4. Trial-fit the **76 × 21 × 18.4 mm holder**, its cell and **both panels**. The measured loaded height is 20.4 mm. Check the actual holder contacts, lead exit and wire clearance; then remove the cell again.
5. Bond the **empty holder** to its flat seat using small accessible fillets suitable for the holder and printed material. Keep glue away from springs, contacts and the lead exit. Do not use the central Ø3.6 hole: safe screw-head space has not been established.
6. Route the holder leads in the rear-side corridor, not through the tiny end gaps. Verify polarity against BAT: pin 1 positive, pin 2 negative. Leave BAT unplugged.

![Small amounts of glue around the ToF on the earlier build](assets/tof-gluing.jpg)

![Check the ToF optical path and surrounding clearance](assets/tof-installed.jpg)

The model allows only about **1.0 mm** of chassis floor and rear motor wall in the tightest regions, plus **0.8 mm** panel pocket skins. Inspect the real print before gluing. The LCD/holder clearance is particularly tight; see the [fit limits](hardware/prints/DESIGN-2026-10-13.md#fit-limits-to-check-on-real-parts).

## 3. Install the external power button

With electronics and cell absent:

1. Feed the rocker from inside outward through its slot, curved pad facing outside and contact tip toward SW1.
2. Slide the keeper from the USB side into its keyed seat.
3. Insert the split pivot pin from the side **away from USB** until its fork seats in the keeper cheek and retains it.
4. Check free movement and spring return. Use the nominal rocker for the first complete-stack fit; do not trim or lengthen its contact to compensate for a wrong stack height.

**Checkpoint:** the mechanism moves freely. Actual switch actuation is checked only after the board stack is seated, with the cell out and USB disconnected.

## 4. Assemble and prewire the board stack

Keep the rear electronics cover outside the body and all power disconnected.

1. Insert **four M2 × 20 socket-head screws from outside the rear cover**. Place the Pi underside on the rails between the stops, with its component side facing inward and its GPIO tails in their relief.
2. Fit **four 11 mm printed spacers**, one at each Pi mounting location on the component side.
3. Align the custom PCB's underside female socket with **all 40 GPIO pins**. Press the boards together squarely, with the custom board's USB-C socket toward the lower rear opening.
4. Fit compact M2 washers and nuts above the custom PCB. Tighten evenly, without bowing either board. Verify actual Pi thickness, soldered-header height and full mating depth: screws must not pull an incompatible header stack into place.
5. Fit the left-angle Micro-USB OTG adapter to the Pi's **USB data port**, then the USB microphone. `PWR IN` does not carry microphone data.
6. Preconnect constrained board-side plugs, particularly **LCD and MENCODER_L**. Leave the remote display ends loose for threading through the neck. H2 and SERVO1 stay empty in the standard build; if using H2 in a modification, its PCB plug also needs connecting before insertion. Leave **BAT** unplugged.
7. Use individual display housings, leave slack for the stack's 4 mm movement and check the actual LCD/left-encoder wire bends against the holder. Keep wires away from the cell, clip, button and panel rims. CAD wire shapes are local clearance references, not a full harness specification.

**Stack, outside to inside:** rear cover → Pi underside → Pi → 11 mm spacers/header gap → custom PCB underside → custom PCB component side.

![Direct Pi-to-PCB header connection; no jumper wires between the boards](assets/direct-pi-connection.jpg)

![Earlier wired board stack; the revision adds four 11 mm supports and outside-entry screws](assets/wired-pcb-on-pi.jpg)

<img src="assets/build-details/usb-microphone-installed.jpg" alt="Left-angle OTG adapter and miniature USB microphone connected to the Pi USB data port" width="760">

## 5. Insert the stack and connect BAT

**Do the first complete mechanical fit with no cell installed and no USB power.**

1. Keep the fixed head and both side panels off. Hold the rear-cover/board stack **4 mm outward** from its final seated plane.
2. Offer it through the open **GPIO side**, slide sideways and align the rear-cover keys while guiding the loose harness. The curved lower lip prevents a simple straight rear insertion.
3. While the stack is still **4 mm outward**, connect **BAT**. Then move the whole stack **4 mm inward**, perpendicular to the boards, to seat its keys. Do not try to unplug or force constrained LCD/H2 plugs with the stack seated.
4. Check that no wire is trapped against the holder, button or rims. **Do not widen the body by centimetres as in the older video.** If it catches, withdraw it and find the interference; do not pull it closed with screws.
5. Still with the cell out and USB disconnected, test the button against the actual SW1: no preload in every orientation, a click before the rocker stop, and full release. The nominal rest gap is **0.20 mm**; the assembly target is **0.15–0.25 mm**. Test through the available play.
6. Try the actual USB cable. Its overmold must pass through the **14 × 8 mm** opening far enough to reach a socket mouth modelled **12.63 mm behind the opening**. A coupon or visible hole does not prove full seating.
7. Once all dry-fit checks and soldering are complete, withdraw the stack **4 mm**, disconnect BAT and slide it out again. With the head still off, fit the cell according to polarity markings, then repeat the side insertion: **BAT at the 4 mm offset → seat inward**. Keep the board switched off while completing the head and remote connections; verify the 5 V output is off before touching signal plugs. If the output is live, stop and disconnect BAT before changing connections.

If the enclosure fit or power-off state is uncertain, continue the remaining fit checks without the cell. Cell installation can wait until the service opening described below. The new charging opening does not change the need to **shut down and switch off before connecting a charger**.

## 6. Fit the fixed head, display and service cover

This design has **no head adjustment, turning or tilting joint**. Fit it only after the electronics stack is seated.

1. Seat an M3 nut in each open-top head ear pocket. Place the carrier on the body.
2. From inside the chassis, fit **two M3 × 12 socket-head screws with 7 mm OD × 0.5 mm washers upward** into the nuts. The battery-side screw enters straight from its open side. On the GPIO side, introduce the screw with its **head tilted about 15° rearward**, then straighten it as the tip enters the roof hole to clear the fixed camera bridge. Tighten gently with a 2.5 mm Allen key.
3. Feed the eight loose display leads and their **individual housings one at a time** through the **Ø12 mm neck**. A rigid eight-wide connector is not assumed to fit. Leave slack and support the PCB near its header while connecting.
4. Place the display on its four locating studs. Use the [LCD wiring table](WIRING.md#display--lcd): PCB pins 1–8 are **VCC, GND, SCL, SDA, RES, DC, CS, BLK**. The display's first two power pins are reversed relative to the custom PCB; match signal labels, not wire colours.
5. Fan the leads away from the cover's four long posts and right-side screw boss. The cover must not press the glass, display flex or components. The model assumes a 1.2 mm display PCB; verify the actual board and free seating before tightening.
6. Insert the M2 nut in the rear-facing right boss. Offer the cover **1.5 mm below** its installed height, move it forward to the rim, then slide it **up 1.5 mm** to engage its two hooks. Secure with **one M2 × 6 socket-head screw**. The hooks slide; do not flex them as snap clips or use the screw to drag the cover into place.

The approximately 24.65 mm cover posts are delicate retainers, not handles. If the actual display differs, check all four corner contacts equally; use small nonconductive shims only on intended corner pads if necessary. Never compensate by loading the glass or forcing the cover.

**Checkpoint:** the fixed head is secure, the display wires remain relaxed, and the service cover meets its rim without force. The earlier bracket/central-screw head photographs are not assembly instructions for this head.

## 7. Fit the camera and close the body

1. Fit the camera **after** tightening the head fasteners so it cannot block the long Allen key. The camera seats move 1 mm forward and the small bores are now **14 mm deep**, not 15 mm. Check actual screws clamp the board before bottoming; keep the lens centred and board flat.
2. Connect the camera ribbon with power off and close both latches. Use a relaxed inverted-S route with broad bends and slack at both connectors. Do not copy sharp folds visible on the prototype or trap the ribbon beside the head screws or panel edges.
3. Position the unchanged speaker behind its grille without loading the diaphragm. Check its two leads against SPEAKER/H3. Connect the remaining remote ends with the board output off and verify all leads against [WIRING.md](WIRING.md).
4. Inspect insulated joints, motor/encoder clearance, holder leads and display slack. There must be enough free lead for the stack's initial 4 mm service movement.
5. Secure the rear cover and both side panels using the matching unchanged attachment hardware. Do not force a panel closed with its screws. Recheck button return, USB plug access, motor retention and camera-ribbon clearance after tightening.

<img src="assets/build-details/camera-mounted-screws.jpg" alt="Earlier camera installation showing mounting screws and the orange ribbon; check revised seat and screw depth" width="650">

<img src="assets/build-details/camera-ribbon-s-bend.jpg" alt="Earlier camera ribbon route; preserve clearance and avoid copying the sharp prototype folds" width="650">

### Rear support foot

Retain the rounded **rear support foot/skid** under the body. With the two wheels it provides the third floor contact and keeps the rear shell off the floor; it is not a powered wheel.

<img src="assets/build-details/rear-support-foot.jpg" alt="Rear support foot below the robot body" width="620">

Set Robbi on a flat surface. Both wheels and the rear foot must support him, with the shell and cables clear of the floor. Recheck the camera image and ToF after closure, then continue with [IMU, motor and voice checks](06-calibration.md). Keep the first powered wheel test raised off the floor.

## Service and disassembly

Shut the Pi down cleanly, switch the board off and disconnect external USB power before opening or changing plugs. Do not treat the printed rocker as a battery-isolation switch.

**Display only:** remove its M2 rear-cover screw, slide the cover **down 1.5 mm**, then pull rearward. Unplug the display leads before lifting the PCB straight off the studs. The head can stay attached.

**Electronics stack or cell:**

1. Remove **both** side panels. Remove the camera module if it obstructs head-fastener access; the CAD tool-access check did not include a measured purchased camera left in place.
2. Open the head cover. Label and unplug display leads **at the display**, free ties and feed their individual housings down the neck. Leave the constrained LCD plug attached to the PCB.
3. Loosen both M3 head screws about **5 mm**, support and lift the head at least **10 mm**. Remove both screw/washer sets completely before sliding the electronics. The battery-side screw withdraws about **12 mm straight down**; the GPIO-side screw withdraws about **10.5 mm**, then its head tilts rearward up to **15°** as the shank leaves the roof. Keep each washer with its screw.
4. If populated for a modification, unplug SERVO1 while the stack is seated. Free other remote harness ends and ties as needed, then release the rear-cover screws.
5. Move the stack **4 mm outward**, **disconnect BAT at that offset**, then slide it out through the GPIO side while guiding the loose harness. Keep constrained board-side LCD, MENCODER_L and any fitted H2 plugs attached until the stack is outside.
6. Release the actual cell-holder spring contact, lift the cell approximately **20 mm**, then withdraw through the battery side. The checked external envelope is up to **70 × Ø18.6 mm**; actual contact travel was not modelled. The bonded empty holder can stay in place.

Reassembly reverses the mechanical route: side entry while 4 mm out → BAT → seat inward → head → camera and panels. Repeat wiring, button and clearance checks. To remove the button pin with the stack out, squeeze its exposed fork tips with fine tweezers and pull straight out; do not force the fork backwards through its bore.
