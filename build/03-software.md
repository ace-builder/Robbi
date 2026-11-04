# Software installation

[Guide home](README.md) · [Complete setup walkthrough](../software/SETUP.md) · [Next: bench tests](04-bench-tests.md)


Target: Raspberry Pi Zero 2 W, Raspberry Pi OS **Bookworm Lite 64-bit (Legacy)**, Python 3.11 or 3.12. Use an empty microSD card for the first install. The installer preserves an existing installation by refusing to overwrite it; it is not an update command.

## 1. Prepare the card and connect

In Raspberry Pi Imager choose the supported OS and set a hostname such as `robbi`, your own Linux username and password, the correct Wi-Fi country, your 2.4 GHz Wi-Fi network/password, and SSH access. These initial settings are needed before SSH can reach the Pi. Boot the Pi and connect using `ssh YOUR_LINUX_USERNAME@robbi.local`, or its router-assigned IP address. Use an interactive terminal, not a background SSH command.

## 2. Run one installer command

Run as your normal Linux user, without putting `sudo` in front:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/ace-builder/Robbi/main/software/install.sh | bash
```

The script resolves a GitHub commit, verifies its bootstrap helper, downloads that pinned source snapshot, and checks every runtime file against the included manifest before making system changes. A separate GitHub Release is not required. Only the runtime is extracted; CAD, source artwork and tests are left off the Pi. Internet access and at least 2 GiB free storage are required. Installation asks for your normal sudo password to install OS packages and the service.

If you already downloaded or cloned the repository, run `bash software/install.sh` from the repository root (or `bash install.sh` inside `software/`). `bash install.sh --plan` describes the steps without installing anything. For a specific published tag or commit, download that version's `software/install.sh` and use `--version TAG_OR_COMMIT`.

## 3. Complete terminal setup

The wizard guides these steps:

1. **Owner:** the name Robbi should use; this is saved only on your Pi.
2. **Dashboard access:** choose a password, or explicitly disable sign-in for a trusted LAN. The dashboard username is `robbi`, separate from your Linux account.
3. **Wi-Fi:** test the current connection and optionally save another network. Its password is hidden; changing networks waits for a deliberate reboot so SSH stays connected.
4. **LLM:** choose OpenAI, OpenRouter or an OpenAI-compatible local server, enter its base URL/model and hidden API key. A local server runs on your own computer; enter its reachable LAN address. An optional small, billable connection test checks the selected model.
5. **Speech:** add a separate speech-recognition key when needed. Optional ElevenLabs setup asks for your own API key, available voice ID and model. No private voice ID or account key is supplied.
6. **Web search:** optionally enter a Tavily key. Free default weather does not need a key. Other app providers can be configured later in the dashboard.
7. **Telegram:** optional. The wizard explains how to create a bot and find your numeric Telegram user ID, then allows only that private chat. Robot actions and photos stay off until enabled separately.
8. **Checks:** test network access and list hardware/audio interfaces, then start the service and show the dashboard address. Missing peripherals do not prevent dashboard startup. No motors move during setup.

API keys go in `~/robbi-v2/.env.secrets` with owner-only permissions; settings in `~/robbi-v2/config/robot.yaml`; memories and generated data in `~/robbi-v2/var/`. These files must never be uploaded. You can rerun `robbi-setup` later or change provider keys/model settings in the dashboard.

## 4. Reboot and finish in the dashboard

Reboot when prompted: `sudo reboot`. SPI, I2C, camera/audio and any newly saved Wi-Fi settings take effect. Open `http://robbi.local:8082` or the router-assigned IP on port 8082, using the chosen dashboard password.

Use Overview to check camera, display, speaker, microphone, front ToF and IMU health. Follow [bench tests](04-bench-tests.md) and [calibration](06-calibration.md). Enroll your own wake phrase in the Wake word tab. Check wheel directions and encoder calibration with the wheels raised before enabling movement. Motors, unsupervised autonomy and wake listening are not enabled blindly by installation.

The public dashboard includes apps, media/audio, manual driving, IMU visualization, experimental mapping, configurable providers and Conversation Lab. It has no Filming tab or private training-recorder routes. Memories and history start empty. Robbi App Hub is a client connection, defaulting to `https://ai-tier.com/apphub`; the Hub website is not installed on the Pi. App permissions require owner approval once and are retained locally.

## Troubleshooting

- Continue an interrupted first install by running the same snapshot's installer again. Completed steps are recorded; a different snapshot refuses to overwrite an existing installation.
- Reopen initial configuration with `robbi-setup`.
- Service status: `systemctl status robbi-v2.service`.
- Logs: `journalctl -u robbi-v2.service -n 80 --no-pager`.
- Restart: `sudo systemctl restart robbi-v2.service`.
- For a wrong model/key, edit Providers in the dashboard or rerun setup; a failed provider does not require reinstalling the OS.
- The installer is validated with offline/mocked setup tests; a fresh-card end-to-end install and your actual hardware still need physical verification.
