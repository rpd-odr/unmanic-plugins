**<span style="color:#56adda">0.1.20-rpd10.3-progress-pipe</span>**
- Explicitly enables FFmpeg `-progress pipe:1` for staged HDR jobs because Unmanic parses the command's stdout, not FFmpeg's default human-readable stderr stats.
- Keeps progress at 99% after FFmpeg encoding and reports 100% only after metadata injection, remux, and final verification succeed.
- Leaves AV1 encoder settings, crop handling, Dolby Vision RPU preparation, HDR10+ injection, and final-container validation unchanged.

**<span style="color:#56adda">0.1.22-rpd10.2-scan-debug</span>**
- Restores the rpd10.2 staged AV1 HDR pipeline and progress parser.
- Adds stage-by-stage diagnostics to the library scan callback; preserves existing filtering and queue-flag logic.
- No changes to AV1 encoding, Dolby Vision RPU, HDR10+ injection, or final-container verification.

**<span style="color:#56adda">0.1.20-rpd10.1</span>**
- Adds FFmpeg machine-readable progress output to the staged AV1 HDR pipeline and parses its elapsed output time.
- Resets progress parser state for each task and uses the video stream's frame rate when available.
- Holds HDR pipeline progress below 100% until metadata injection and final-container verification complete.
- Does not change encoder settings, crop handling, Dolby Vision RPU preparation, HDR10+ injection, or remux validation.

**<span style="color:#56adda">0.1.20-rpd9</span>**
- Fixes AV1 Dolby Vision + HDR10+ playback speed by restoring the source constant frame rate when extracting AV1 to IVF and forcing the same exact rational frame rate with MKVToolNix `--default-duration`.
- Rejects variable-frame-rate sources in the combined DV + HDR10+ remux path instead of assigning an incorrect constant frame duration.
- Keeps final Dolby Vision Profile 10 BL+RPU and HDR10+ per-frame metadata verification before publishing the output.

**<span style="color:#56adda">0.1.20-rpd8</span>**
- Fixes the DV + HDR10+ final remux: use MKVToolNix to parse AV1 Dolby Vision RPU metadata OBUs and write the Matroska `dvvC` configuration record instead of asking FFmpeg to reconstruct it from IVF.
- Keeps strict final verification of Profile 10, BL+RPU, and HDR10+ frame count; the output is not published if Dolby Vision signaling is missing.
- Requires `mkvmerge` in the Unmanic container for the combined DV + HDR10+ path.
- Fixes HDR10+ false negatives when FFprobe omits SMPTE ST 2094-40 side data by falling back to `hdr10plus_tool_av1` bitstream extraction.
- Passes the source path into the HDR10+ detector and reuses the resolved extractor path in the staged AV1 metadata pipeline.
- Fixes Dolby Vision crop validation to inspect the extracted RPU, then verifies the crop-adjusted RPU frame count and zero Level 5 offsets.

**<span style="color:#56adda">0.1.20-rpd4</span>**
- Restores the Dolby Vision configuration record after HDR10+ AV1 IVF remux using `dovi_rpu=compression=none`.
- Verifies the final MKV advertises Dolby Vision Profile 10 BL+RPU with compatibility ID 1.
- Re-extracts HDR10+ from the final MKV and checks the metadata frame count after remux.
- Restricts the combined DV + HDR10+ remux to single-video-stream sources and publishes the remux only after validation.
- Replaces the old `dovi_rpu` passthrough path with a crop-aware Dolby Vision P8.1 RPU preparation stage.
- Adds HDR10+ extraction and post-encode ITU-T T.35 injection into AV1.
- Adds strict frame-count verification; metadata is never silently duplicated or truncated.
- Requires `dovi_tool` for cropped DV P8.1 and an AV1-enabled `hdr10plus_tool_av1` for HDR10+ sources.
- Leaves Profile 7.6 unsupported.

**<span style="color:#56adda">0.1.20-rpd2</span>**
- Explicitly enables SVT-AV1 Dolby Vision RPU coding with `-dolbyvision 1` when RPU metadata is detected.
- Keeps the stream-specific `dovi_rpu` bitstream filter and leaves non-Dolby-Vision sources unchanged.
- Scope remains limited to the tested Dolby Vision Profile 8 path; Profile 7.6 is not enabled or claimed as supported.

**<span style="color:#56adda">0.1.20-rpd1</span>**
- Custom fork based on upstream 0.1.20.
- Added optional **Add Dolby Vision to AV1** setting for Standard mode.
- Detects actual Dolby Vision RPU metadata before applying FFmpeg's dovi_rpu bitstream filter.
- Preserves Dolby Vision RPU when transcoding compatible Dolby Vision sources to AV1 with SVT-AV1.
- Keeps existing smart black-bar detection/cropping and resolution scaling behavior.
- Sources without Dolby Vision RPU use the normal AV1 transcoding path.

**<span style="color:#56adda">0.1.20</span>**
- Corrected encoder options setting in plugin stream mapper for libsvtav1
- Fix the libsvtav1 encoder integration so the stream mapper uses the correct encoder args contract

**<span style="color:#56adda">0.1.19</span>**
- adds the -reinit_filter:0 option for all decode modes of vaapi and qsv when h/w encode is used.
- Needed since vaapi & qsv use hwupload to get the frame into GPU memory and will generate an error if reinit_filter is not set to 0

**<span style="color:#56adda">0.1.18</span>**
- adds similar logic for qsv and vaapi as was done for nvenc in 0.1.17
- adds new qsv_safe_decode and vaapi_safe_decode options to the respective encoders
- adds new paths in their respective define_filtergraph functions to move the frames off the GPU to avoid h/w accel failure due to midstream colorspace changes
- adds '-reinit_filter 0' option to the ffmpeg command line when the x_safe_option is enabled so that ffmpeg can ignore the filter change that would have resulted from the colorspace change
- the combination of these allows ffmpeg to successfully process the file rather than failing

**<span style="color:#56adda">0.1.17</span>**
- adds nvenc_safe_decode option to allow plugin to avoid an error caused by colorspace changes in the file
- adds new path in the define_filtergraph function to move the frames off the GPU to avoid h/w accel failure due to midstream colorspace changes
- adds '-reinit_filter 0' option to the ffmpeg command line so that ffmpeg can ignore the filter change that would have resulted from the colorspace change
- the combination of these allows ffmpeg to successfully process the file rather than failing
 
**<span style="color:#56adda">0.1.16</span>**
- Add support for using the File Metadata helper for storing details on moved files (Requires Unmanic v0.3.0)
- Fix issue with settings not chaing encoder when the codec changes

**<span style="color:#56adda">0.1.15</span>**
- Add a warning against using "Prefer Quality" when scaling a video down
- Add smart output target support for QSV
- Reduce sample time for black-box detection to 10 seconds of video
- Fix smart output target bitrate derivation to prefer video-only stream stats (Matroska `BPS`) and packet sampling over container bitrate
- Add ffprobe sampling as a fallback option when bitrate is not specified on a video stream
- Add more detailed ouput to worker logs as the plugin processes and generates the ffmpeg command to be run

**<span style="color:#56adda">0.1.14</span>**
- Enable smart filters and resolution scaling in basic mode (works only with NVENC at this stage)
- Added new smart output target controls to basic mode, including goal presets (Prefer Quality/Balanced/Prefer Compression)

**<span style="color:#56adda">0.1.13</span>**
- Fix bug where intermittently HW accelerated config was reverted to CPU encoding on its own

**<span style="color:#56adda">0.1.12</span>**
- Improved handling for HDR content with new helper tools for detecting and parsing metadata.
- Removed the look-ahead feature from QSV's HEVC and AV1 encoders (not supported).
- Fixed an issue with default tune options on libx264 and libx265
- Removed the tune option from QSV encoders (not supported).
- Changed the VAAPI hardware decoding setting to now be a dropdown menu instead of a checkbox (like all the other encoders).
- Speed up crop-detect on smaller files.

**<span style="color:#56adda">0.1.11</span>**
- Fix CQP quality selector for VAAPI encoding

**<span style="color:#56adda">0.1.10</span>**
- Adds better management of 10Bit video formats for NVIDIA, Intel, and AMD hardware. 
- Adds an improved fallback system to prevent failed decodes for QSV.
- The video scaling smart filter has been improved to correctly work with custom resolutions and different aspect ratios.
- A new debugging tool has been added to make troubleshooting and development easier.

**<span style="color:#56adda">0.1.9</span>**
- Add some safety rails to the black-bar detection so we ignore bars of a few px

**<span style="color:#56adda">0.1.8</span>**
- Improvements to black-bar detection

**<span style="color:#56adda">0.1.7</span>**
- Add support for the STV-AV1 encoder

**<span style="color:#56adda">0.1.6</span>**
- Fix bug causing files to be perpetually added to the task queue if mode is set to advanced, but the smart filters were previously applied

**<span style="color:#56adda">0.1.5</span>**
- Fix video scaling smart filter for videos that do not use a 16:9 aspect ratio

**<span style="color:#56adda">0.1.4</span>**
- add missing 'generic_kwargs' from return statement in nvenc.py basic config section

**<span style="color:#56adda">0.1.3</span>**
- fix nvenc 10 bit profile name

**<span style="color:#56adda">0.1.2</span>**
- Fix for plugin updates from versions older than 0.1.0

**<span style="color:#56adda">0.1.1</span>**
- Add support for the av1_qsv encoder

**<span style="color:#56adda">0.1.0</span>**
- Stable release
- Prefix QSV config options in plugin settings file to isolate them from libx encoder settings (users will need to reconfigure some QSV settings)
- Prefix VAAPI config options in plugin settings file to isolate them from libx encoder settings (users will need to reconfigure some VAAPI settings)

**<span style="color:#56adda">0.0.10</span>**
- Add support for QSV HW accelerated decoding
- Add support for the scale_qsv filter when using qsv encoding
- Add support for the scale_cuda filter when using nvenc

**<span style="color:#56adda">0.0.9</span>**
- Add support for the h264_vaapi encoder
- Add support for the h264_nvenc encoder
- Add support for the hevc_nvenc encoder

**<span style="color:#56adda">0.0.8</span>**
- update ffmpeg helper library to latest version

**<span style="color:#56adda">0.0.7</span>**
- Handle circumstance where file probe has no 'codec_name'
- Improve library scan speed when used with other plugins that use ffprobe

**<span style="color:#56adda">0.0.6</span>**
- Fix an error in ffmpeg command generator

**<span style="color:#56adda">0.0.5</span>**
- Improvements to ffmpeg command generator
- Fix issue where input file was added before additional main options

**<span style="color:#56adda">0.0.4</span>**
- Add ability to specify a codec in plain text in advanced mode

**<span style="color:#56adda">0.0.3</span>**
- Fix bug where videos would forever be re-added to the task list if force transcoding was enabled

**<span style="color:#56adda">0.0.2</span>**
- Fix detection if video stream is already in the correct codec
- Add ability to strip data and attachment streams from video files

**<span style="color:#56adda">0.0.1</span>**
- Add an "Advanced" configuration option to the plugin's settings
- Add ability to force transcoding of a video when it is already in the desired video codec

**<span style="color:#56adda">0.0.1-beta2</span>**
- Add support for specifying the VAAPI device
- Improvements to code that generates the encoder specific arguments

**<span style="color:#56adda">0.0.1-beta1</span>**
- Initial version
