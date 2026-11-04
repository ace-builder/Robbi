# Robbi App Hub

Open **Robbi App Hub** in the dashboard to browse and install apps. The website
and catalog server are hosted separately; neither is installed on the robot.

The connection form defaults to `https://ai-tier.com/apphub`. Change its default
in **Settings → App Hub**, or in your private configuration:

```yaml
app_hub:
  url: https://ai-tier.com/apphub
```

The URL must serve Robbi's signed catalog protocol under `/api/v1/`. Entering a
URL does not trust a signing key or install an app. Check the Hub, review its key,
then connect. A trusted connection keeps its own address until you reconnect;
changing the default does not silently replace that trust. Local HTTP is an
explicit development option in the connection form; public hosting uses HTTPS.

An unavailable Hub leaves installed apps and robot startup working. Downloads,
signatures, permissions, version checks and per-app state are handled on Robbi.
The URL is a configured destination, not a claim that a server is already live.

Current setup and isolation contract: [security and review notes](security-and-review.txt). Imported apps cannot bypass OS isolation with the SDK development permission switch.
