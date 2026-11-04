# Public media boundary

`approved-media.json` lists the exact reviewed bytes. Runtime art was reviewed using contact sheets; generated video hashes were matched to the asset catalog, and generic audio was matched to synthesis/generation records and reviewed prompts. Assembly illustrations show the robot and building steps, not robot-camera captures or personal portraits. Robot models contain geometry, not runtime calibration.

Excluded: the owner's camera captures, logs, memories, voice enrollment, raw training data, filming takes, named personal speech exports, and raw generation metadata containing local paths or account voice IDs. Historical vocal clips without verified provenance remain private. Startup/reaction aliases use existing CC0 emotion effects instead; this changes their sound in the public build without changing the owner's installed robot.

The generic library includes generated robot expressions, seasonal cartoons and short nonpersonal lines. The configured voice ID is empty in public metadata; new live speech requires the new owner's API key and voice. Account-generated output is not marked CC0. The proposed first-party licences are described in the repository LICENSE.md. These do not replace third-party or provider terms; no CC0 status is assigned to account-generated output.

`asset-credits.json` records the three CC0 music excerpts and generated reaction-cue provenance without private voice identifiers. `asset-licenses/` preserves available source notices. Emotion effects use the documented CC0 synthesis/Kenney sources. Bundled Three.js files retain their license in `web/motion-studio/vendor/THREE-LICENSE.txt`.

This is an allowlist for this reviewed snapshot, not permission to add arbitrary files to `assets/`. Regenerated media requires a fresh review and updated hash.

Old standalone spoken passages, filming dialogue and personal voice samples stay
private. Short robot reactions such as `okay.wav` and `grumpy.wav` remain in the
public sound library. The release builder rejects voice-export folders and
filming/speech-take audio even if someone adds them to the media allowlist.
Do not copy audio from a running robot or a personal export into this source tree.

The source artwork, original CC0 music and documented nonverbal generated effects are included for builders. App icons and display cards match their reviewed generation hashes. Editable masters stay in source-only folders and are excluded from the Raspberry Pi runtime manifest. No private cloned-voice masters or old standalone dialogue takes are bundled.
