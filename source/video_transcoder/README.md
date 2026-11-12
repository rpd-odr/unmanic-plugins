# Transcode Video Files

Custom fork of the official Unmanic Transcode Video Files plugin.

This fork adds a metadata-aware AV1 path for supported Dolby Vision Profile 8.1 and HDR10+ sources.

## AV1 dynamic metadata

When Config mode is Standard, Video Codec is AV1, and **Preserve Dolby Vision and HDR10+ in AV1** is enabled, the plugin:

- Detects actual Dolby Vision RPU metadata and only enables the tested Profile 8.1 / BL compatibility 1 path.
- For cropped Dolby Vision sources, extracts the source HEVC and RPU separately, verifies that Level 5 offsets exactly match the detected symmetric crop, clears them with the validated `dovi_tool -c extract-rpu` mode, verifies that the adjusted RPU retains the same frame count and has zero Level 5 offsets, then injects it into a temporary HEVC elementary stream for AV1 decoding.
- Enables SVT-AV1 Dolby Vision coding with `-dolbyvision 1`; it does not rely on the `dovi_rpu` bitstream filter passthrough.
- Detects HDR10+ through FFprobe side data and falls back to `hdr10plus_tool_av1` bitstream extraction when FFprobe omits SMPTE ST 2094-40 side data.
- Extracts source HDR10+ metadata and injects it into the encoded AV1 stream as ITU-T T.35 metadata OBUs before final remuxing.
- Verifies that source HDR10+ metadata and encoded AV1 frame counts match. It fails the task rather than duplicating or truncating dynamic metadata.
- For combined DV + HDR10+, remuxes the injected AV1 IVF with MKVToolNix 79.0 or newer. Its AV1 parser reads the Dolby Vision RPU metadata OBUs and writes the Matroska Dolby Vision configuration mapping; the pipeline then verifies Profile 10 BL+RPU / compatibility ID 1 and re-extracts HDR10+ from the final MKV.
- The combined DV + HDR10+ remux currently requires a single video stream; all final checks run before the remuxed file replaces the encoded output.
- Leaves files without the supported metadata on the normal FFmpeg path.

Profile 7.6 is deliberately not enabled by this feature.

## Runtime tools

The tools must be available **inside the Unmanic container**, not only on the Fedora host:

- `ffmpeg` and `ffprobe`
- `dovi_tool` when a supported DV source needs crop-aware RPU preparation
- `mkvmerge` 79.0 or newer for combined Dolby Vision + HDR10+ sources
- `hdr10plus_tool_av1`, an AV1-capable build of [sven-pke/hdr10plus_tool](https://github.com/sven-pke/hdr10plus_tool), when the source contains HDR10+

The ordinary HEVC-only `hdr10plus_tool` build is not sufficient for the AV1 injection stage. The AV1-enabled build retains HEVC extraction and adds AV1 IVF injection.

## Versioning

Custom releases use the `-rpdN` suffix. Current development version: `0.1.20-rpd10`.

## Upstream

- [Official Unmanic plugin](https://github.com/Unmanic/plugin.video_transcoder)
- [Unmanic](https://github.com/Unmanic/Unmanic)
