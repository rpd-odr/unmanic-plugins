# Transcode Video Files

This is a custom fork of the Unmanic Transcode Video Files plugin.

## Dolby Vision and HDR10+ to AV1

In Standard mode, with AV1 selected and **Preserve Dolby Vision and HDR10+ in AV1** enabled, the plugin detects dynamic metadata in the source.

For supported Dolby Vision Profile 8.1 sources, the plugin enables SVT-AV1 Dolby Vision coding. When black-bar cropping is active, it prepares an intermediate HEVC elementary stream with crop-adjusted Dolby Vision RPU Level 5 active-area metadata before decoding and encoding.

For HDR10+ sources, the plugin extracts the source SMPTE ST 2094-40 metadata and injects the corresponding ITU-T T.35 metadata OBUs into the encoded AV1 bitstream. The AV1 stream is then remuxed with the original output's audio, subtitles, chapters, attachments, and other streams.

The plugin checks frame counts and fails safely if metadata cannot be aligned with the encoded AV1 frames. It does not silently duplicate or truncate HDR10+ metadata.

After the HDR10+ remux, the plugin restores and verifies the Dolby Vision Profile 10 BL+RPU configuration when DV is present, then re-extracts HDR10+ from the final MKV before publishing the output. The combined DV + HDR10+ path currently requires a single video stream.

Dolby Vision Profile 7.6 is not enabled by this feature.

## Runtime requirements

Required executables must be available inside the Unmanic container:

- `ffmpeg` and `ffprobe`
- `dovi_tool` for crop-aware Dolby Vision RPU preparation
- `hdr10plus_tool_av1`, an AV1-capable build of [sven-pke/hdr10plus_tool](https://github.com/sven-pke/hdr10plus_tool), for HDR10+ extraction and AV1 injection

The standard HEVC-only `hdr10plus_tool` binary cannot perform the AV1 injection stage.

## Support

- [Custom fork](https://github.com/rpd-odr/plugin.video_transcoder)
- [Official plugin](https://github.com/Unmanic/plugin.video_transcoder)
- [Unmanic](https://github.com/Unmanic/Unmanic)
