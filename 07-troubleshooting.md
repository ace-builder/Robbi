# 7. Troubleshooting

![Troubleshooting](assets/banners/07-troubleshoot.png)

[Guide home](README.md) · [← Calibration](06-calibration.md)

Power off and remove the cell before moving plugs, swapping wires or touching the CSI ribbon. Inspect logs and live values before changing several settings at once.

| Symptom | Check first | Next step |
|---|---|---|
| Pi does not boot | Press the PCB power button; then check battery polarity, loaded supply voltage, microSD image and full header alignment | Isolate the Pi from the custom board and test it with its proper USB supply |
| Pi resets when motors start | Power rail droop, cell/holder contacts, motor startup load, protection trips | Stop motor tests and measure the loaded rails; do not disable protection |
| Board gets hot or a supply is shorted | Power immediately off; solder bridges, offset header, reversed lead | Correct the hardware fault before another power attempt |
| Dashboard unavailable | Runtime/service status, Wi-Fi and the Pi’s current address | Check `journalctl -u robbi-v2.service -n 80 --no-pager`; confirm selected config and port |
| Installer says no runtime release | Only the bootstrap script has been published so far | Wait for the runtime release; it makes no system changes while unavailable |
| Installer interrupted | Rerun the same installer/release | Completed steps resume; do not run this fresh installer over an unrelated existing robot |
| Setup rejects OS | Check `/etc/os-release` and `python3 --version` | Use Bookworm Lite 64-bit; Trixie/Python 3.13 is not supported yet |
| New Wi-Fi does not connect | Correct 2.4 GHz SSID/password and a deliberate reboot | The wizard tests the current connection only; restore the original Imager network if needed |
| LLM test fails | Run `robbi-setup` and check key/model/account access | HTTP 401 means credentials, 404 model/endpoint, 429 quota/rate limit; the dashboard still works offline |
| Import or dependency error | Python version and the active `.venv` | Use the Bookworm/Python 3.11 environment and `pip check`; preserve the apt camera/numerical stack |
| Display lights but no face | LCD signal order, SPI device, D/C and reset | Check runtime logs and the ST7789 driver; do not assume backlight means SPI is wired correctly |
| Face is clipped or shifted | Exact panel variant and runtime dimensions | Verify 240 × 280 hardware, runtime 280 × 240 and offset_left 20 |
| Camera not detected | Power-off ribbon seating, contact direction and adapter type | Test `rpicam-hello --list-cameras` with Robbi stopped; follow the camera manufacturer's overlay instructions |
| Camera is detected but image is dark/blocked | Lens cap/film, enclosure opening and exposure | Inspect a saved still with the head open |
| USB microphone missing | USB data port and OTG adapter | Check `arecord -l`; a charge-only adapter or PWR IN port will not work |
| Speech is silent | ALSA card name, speaker connector, overlay and volume | Check `aplay -l`; confirm the amplifier overlay does not claim motor GPIO4 |
| Speech distorts or rattles | Excess volume, wrong speaker impedance, grille pressure | Lower software volume and inspect the 8 Ω / 0.5 W speaker mount |
| Typed chat works but voice does not | Capture sample, STT provider and wake references | Test recorded audio first, then speech recognition, then wake detection |
| ToF absent or stale | H1 wiring, XSHUT13 and runtime status | H1 boots at 0x29 and is reassigned to 0x30; keep unused H2 disabled |
| IMU view does not move | Correct chip, I²C identity and fresh samples | Check live values and follow calibration in the dashboard’s IMU tab; verify the module identity before changing hardware |
| Motor output refused | Both runtime motor gates and a fresh forward-range sample | Finish the wiring/bench checks before enabling output; do not bypass the guard for floor tests |
| One wheel turns, the other does not | Six-wire motor harness and correct side assignment | Check motor output separately from encoder A/B feedback |
| Encoder count stays zero | Supply, ground, both A/B pins and software pin map | Reconcile GPIO16 versus the original robot's GPIO7 repair; do not bridge pins |
| Robot spins instead of driving straight | Left/right mapping, direction and encoder signs | Raised-wheel test one side at a time; change one setting at a time |
| Consistent distance error | Wheel circumference and decoded counts per wheel revolution | Re-measure and calibrate one distance scale parameter |
| Irregular drift or different turns each time | Wheel rubbing, traction, loose hubs, missed counts and voltage | Fix the mechanical/electrical cause before retuning the controller |
| Works open, fails closed | Pinched ribbon, strained plug, screw contact or wheel obstruction | Reopen, inspect and reroute; do not force the panel shut |

When reporting a fault, include the board/print/software versions, the exact symptom, relevant log lines and a clear connector photo. Remove API keys, Wi-Fi credentials and personal recordings before sharing diagnostics.
