#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metadata-aware AV1 worker wrapper for Unmanic.

The wrapper keeps the plugin's generated FFmpeg command intact, with two
surgical additions:
  * when a DV P8.1 source is cropped, inject a crop-adjusted RPU into a
    temporary HEVC elementary stream and use that stream for video decoding;
  * when HDR10+ is present, inject its per-frame T.35 metadata into the
    encoded AV1 IVF stream before remuxing the final container;
  * when Dolby Vision is present without HDR10+, finalize the AV1 container
    through MKVToolNix so its Dolby Vision dvvC configuration is rebuilt.

Required tools are discovered inside the Unmanic container:
  ffmpeg, ffprobe
  dovi_tool (only for cropped DV P8.1)
  hdr10plus_tool_av1 (AV1-capable build of quietvoid/hdr10plus_tool)
"""

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from fractions import Fraction


def log(message):
    print("[AV1 metadata] " + str(message), file=sys.stderr, flush=True)


def tool(name, env_name=None):
    candidate = os.environ.get(env_name, "") if env_name else ""
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError("Required executable '{}' was not found in the Unmanic container PATH".format(name))


def require_mkvmerge():
    executable = tool("mkvmerge", "MKVMERGE")
    version_text = run_checked(
        [executable, "--version"],
        "Checking MKVToolNix version for AV1 Dolby Vision muxing",
        capture=True
    )
    match = re.search(r"v(\d+)\.(\d+)", version_text)
    if not match:
        raise RuntimeError("Could not determine MKVToolNix version: {}".format(version_text.strip()))
    version = (int(match.group(1)), int(match.group(2)))
    if version < (79, 0):
        raise RuntimeError(
            "AV1 Dolby Vision remux requires MKVToolNix 79.0 or newer; found {}".format(
                version_text.strip()
            )
        )
    return executable


def run_checked(command, label, capture=False):
    log(label)
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
        check=False,
    )
    if capture:
        if result.stdout:
            for line in result.stdout.splitlines():
                log(line)
    if result.returncode:
        raise RuntimeError("{} failed with exit code {}".format(label, result.returncode))
    return result.stdout if capture else ""


def ffprobe_json(ffprobe, path, extra=()):
    command = [ffprobe, "-v", "error"] + list(extra) + ["-of", "json", path]
    output = subprocess.check_output(command, text=True)
    return json.loads(output)


def get_video_info(ffprobe, source):
    data = ffprobe_json(
        ffprobe,
        source,
        ("-select_streams", "v:0", "-show_entries",
         "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,start_time,nb_frames,nb_read_frames")
    )
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError("No video stream found in source")
    return streams[0]


def get_constant_frame_rate(info):
    average = info.get("avg_frame_rate")
    nominal = info.get("r_frame_rate")
    invalid = (None, "", "0/0", "N/A")

    if average in invalid and nominal in invalid:
        raise RuntimeError("Could not determine source frame rate for AV1 metadata remux")

    try:
        average_rate = Fraction(average) if average not in invalid else Fraction(nominal)
        nominal_rate = Fraction(nominal) if nominal not in invalid else average_rate
    except (ValueError, ZeroDivisionError):
        raise RuntimeError(
            "Invalid source frame rate: avg_frame_rate={}, r_frame_rate={}".format(
                average, nominal
            )
        )

    if average_rate <= 0 or nominal_rate <= 0:
        raise RuntimeError("Source frame rate must be greater than zero")

    if average_rate != nominal_rate:
        raise RuntimeError(
            "Variable-frame-rate source detected (avg_frame_rate={}, r_frame_rate={}); "
            "refusing to assign a constant IVF/MKV frame duration".format(average, nominal)
        )

    return average_rate


def get_video_stream_count(ffprobe, source):
    # Matroska cover art is exposed by FFprobe as a video stream with
    # disposition.attached_pic=1. It is copied from the original input and
    # must not make the DV crop/remux path look like a multi-video source.
    data = ffprobe_json(
        ffprobe, source,
        ("-select_streams", "v",
         "-show_entries", "stream=index:stream_disposition=attached_pic")
    )
    streams = data.get("streams", [])
    return sum(
        1 for stream in streams
        if not stream.get("disposition", {}).get("attached_pic", 0)
    )


def count_video_packets(ffprobe, path, input_format=None):
    command = [ffprobe, "-v", "error"]
    if input_format:
        command += ["-f", input_format]
    command += [
        "-count_packets", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_packets",
        "-of", "default=noprint_wrappers=1:nokey=1", path
    ]
    output = subprocess.check_output(command, text=True).strip()
    if not output or output == "N/A":
        raise RuntimeError("Could not determine video packet count for {}".format(path))
    return int(output)


def extract_hdr10plus(hdr_tool, source, json_path):
    run_checked(
        [hdr_tool, "extract", source, "-o", json_path],
        "Extracting source HDR10+ metadata"
    )
    with open(json_path, "r", encoding="utf-8") as stream:
        metadata = json.load(stream)
    scenes = metadata.get("SceneInfo")
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("HDR10+ extractor returned an empty or invalid SceneInfo array")
    return len(scenes)


def validate_crop_against_rpu(dovi_tool, rpu_path, crop_value, source_info):
    try:
        crop_w, crop_h, crop_x, crop_y = [int(value) for value in crop_value.split(":")]
    except (AttributeError, TypeError, ValueError):
        raise RuntimeError("Invalid detected crop value: {}".format(crop_value))

    source_w = int(source_info.get("width") or 0)
    source_h = int(source_info.get("height") or 0)
    crop_right = source_w - crop_w - crop_x
    crop_bottom = source_h - crop_h - crop_y

    # The tested RPU edit only supports symmetric top/bottom letterbox crops.
    if crop_w != source_w or crop_x != 0 or crop_y != crop_bottom:
        raise RuntimeError(
            "The tested DV RPU crop edit only supports symmetric top/bottom letterbox crops; "
            "detected crop is {}".format(crop_value)
        )

    summary = run_checked(
        [dovi_tool, "info", "-s", rpu_path],
        "Checking Dolby Vision Level 5 active-area offsets",
        capture=True
    )
    match = re.search(
        r"L5 offsets:\s*top=(\d+)(?:\.\.(\d+))?,\s*bottom=(\d+)(?:\.\.(\d+))?"
        r"(?:,\s*left=(\d+)(?:\.\.(\d+))?,\s*right=(\d+)(?:\.\.(\d+))?)?",
        summary,
        flags=re.IGNORECASE
    )
    if not match:
        raise RuntimeError("Could not determine Dolby Vision Level 5 offsets from source RPU")

    top_max = int(match.group(2) or match.group(1))
    bottom_max = int(match.group(4) or match.group(3))
    left_max = int(match.group(6) or match.group(5) or 0)
    right_max = int(match.group(8) or match.group(7) or 0)
    resolved_crop = crop_value
    if top_max != crop_y or bottom_max != crop_bottom or left_max or right_max:
        # cropdetect reports chroma-aligned boundaries, while Dolby Vision L5
        # active-area offsets can be one luma pixel more precise. Only accept
        # the narrowly defined symmetric +/-1 px case, and use the RPU values
        # as the source of truth for the actual crop coordinates.
        one_pixel_symmetric_difference = (
            not left_max and not right_max
            and abs(top_max - crop_y) == 1
            and abs(bottom_max - crop_bottom) == 1
            and top_max == bottom_max
        )
        if not one_pixel_symmetric_difference:
            raise RuntimeError(
                "Detected crop ({}, {}) does not match DV L5 offsets "
                "(top={}, bottom={}, left={}, right={}); refusing to rewrite RPU".format(
                    crop_y, crop_bottom, top_max, bottom_max, left_max, right_max
                )
            )
        resolved_height = source_h - top_max - bottom_max
        if resolved_height <= 0 or resolved_height % 2:
            raise RuntimeError(
                "DV L5 offsets ({}, {}) produce an invalid 4:2:0 crop height {}; "
                "refusing to adjust crop".format(top_max, bottom_max, resolved_height)
            )
        resolved_crop = "{}:{}:0:{}".format(source_w, resolved_height, top_max)
        log(
            "WARNING: cropdetect differs from symmetric DV L5 offsets by exactly "
            "one pixel per edge; using RPU-aligned crop {} instead of {}".format(
                resolved_crop, crop_value
            )
        )
    frames_match = re.search(r"^\s*Frames:\s*(\d+)\s*$", summary, flags=re.MULTILINE)
    if not frames_match:
        raise RuntimeError("Could not determine source RPU frame count")
    return int(frames_match.group(1)), resolved_crop


def apply_resolved_crop(ffmpeg_args, detected_crop, resolved_crop):
    if detected_crop == resolved_crop:
        return list(ffmpeg_args)

    old_filter = "crop={}".format(detected_crop)
    new_filter = "crop={}:exact=1".format(resolved_crop)
    updated_args = []
    replacements = 0
    for argument in ffmpeg_args:
        if isinstance(argument, str) and old_filter in argument:
            replacements += argument.count(old_filter)
            argument = argument.replace(old_filter, new_filter)
        updated_args.append(argument)

    if replacements != 1:
        raise RuntimeError(
            "Expected exactly one generated FFmpeg crop filter '{}' when applying "
            "the DV L5-aligned crop, found {}; refusing to encode".format(
                old_filter, replacements
            )
        )
    log("Updated generated FFmpeg crop filter to {}".format(new_filter))
    return updated_args


def prepare_dovi_hevc(ffmpeg, dovi_tool, source, workdir, crop_value, source_info):
    source_hevc = os.path.join(workdir, "source.hevc")
    source_rpu = os.path.join(workdir, "source.rpu")
    edited_rpu = os.path.join(workdir, "crop_adjusted.rpu")
    prepared_hevc = os.path.join(workdir, "prepared.hevc")

    run_checked(
        [
            ffmpeg, "-hide_banner", "-y", "-i", source,
            "-map", "0:v:0", "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", source_hevc
        ],
        "Extracting source HEVC elementary stream"
    )
    run_checked(
        [dovi_tool, "extract-rpu", "-i", source_hevc, "-o", source_rpu],
        "Extracting original Dolby Vision RPU for crop validation"
    )

    source_rpu_frames, resolved_crop = validate_crop_against_rpu(
        dovi_tool, source_rpu, crop_value, source_info
    )

    # This legacy crop-adjustment mode was validated against the source MKV:
    # it clears Level 5 active-area offsets while retaining the RPU metadata.
    run_checked(
        [dovi_tool, "-c", "extract-rpu", "-i", source, "-o", edited_rpu],
        "Extracting and clearing Dolby Vision Level 5 active-area offsets"
    )

    adjusted_summary = run_checked(
        [dovi_tool, "info", "-s", edited_rpu],
        "Verifying crop-adjusted Dolby Vision RPU",
        capture=True
    )
    frames_match = re.search(
        r"^\s*Frames:\s*(\d+)\s*$", adjusted_summary, flags=re.MULTILINE
    )
    offsets_match = re.search(
        r"L5 offsets:\s*top=(\d+).*?bottom=(\d+).*?left=(\d+).*?right=(\d+)",
        adjusted_summary,
        flags=re.IGNORECASE
    )
    if not frames_match or int(frames_match.group(1)) != source_rpu_frames:
        adjusted_frames = int(frames_match.group(1)) if frames_match else "unknown"
        raise RuntimeError(
            "Crop-adjusted RPU frame count {} differs from source {}".format(
                adjusted_frames, source_rpu_frames
            )
        )
    if not offsets_match:
        raise RuntimeError("Could not verify Level 5 offsets in crop-adjusted RPU")
    adjusted_offsets = tuple(int(value) for value in offsets_match.groups())
    if adjusted_offsets != (0, 0, 0, 0):
        raise RuntimeError(
            "Crop-adjusted RPU still contains non-zero Level 5 offsets: {}".format(
                adjusted_offsets
            )
        )

    run_checked(
        [
            dovi_tool, "inject-rpu", "-i", source_hevc,
            "--rpu-in", edited_rpu, "-o", prepared_hevc
        ],
        "Injecting crop-adjusted Dolby Vision RPU into HEVC"
    )
    return prepared_hevc, resolved_crop


def add_prepared_hevc_input(ffmpeg_args, source, prepared_hevc, frame_rate, start_time):
    args = list(ffmpeg_args)
    source_input = None
    for index in range(len(args) - 1):
        if args[index] == "-i" and args[index + 1] == source:
            source_input = index
            break
    if source_input is None:
        raise RuntimeError("Could not locate the original input in the generated FFmpeg command")

    # The original file remains input 0 for audio/subtitles/attachments.
    # The prepared elementary stream becomes input 1 for the primary video.
    input_options = []
    try:
        offset = float(start_time or 0)
    except (TypeError, ValueError):
        offset = 0
    if offset:
        input_options += ["-itsoffset", str(offset)]
    input_options += ["-r", frame_rate, "-f", "hevc", "-i", prepared_hevc]
    args[source_input + 2:source_input + 2] = input_options

    for index, value in enumerate(args):
        if value == "-map" and index + 1 < len(args):
            mapping = args[index + 1]
            if mapping in ("0:v:0", "0:v:0?"):
                args[index + 1] = mapping.replace("0:v:0", "1:v:0", 1)
        elif value == "-filter_complex" and index + 1 < len(args):
            args[index + 1] = re.sub(r"(?<![0-9])0:v:", "1:v:", args[index + 1])

    return args


def run_encode(ffmpeg, args):
    command = [ffmpeg] + list(args)
    log("Running the plugin-generated FFmpeg encode command")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
    )
    try:
        if process.stdout:
            for line in process.stdout:
                # Preserve FFmpeg -progress pipe:1 output for Unmanic's parser.
                sys.stdout.write(line)
                sys.stdout.flush()
    finally:
        if process.stdout:
            process.stdout.close()
    code = process.wait()
    if code:
        raise RuntimeError("FFmpeg encode failed with exit code {}".format(code))


def ivf_frame_count(path):
    count = 0
    with open(path, "rb") as stream:
        header = stream.read(32)
        if len(header) != 32 or header[:4] != b"DKIF" or header[8:12] != b"AV01":
            raise RuntimeError("FFmpeg did not produce a valid AV1 IVF stream")
        while True:
            frame_header = stream.read(12)
            if not frame_header:
                break
            if len(frame_header) != 12:
                raise RuntimeError("Truncated IVF frame header")
            size = struct.unpack_from("<I", frame_header, 0)[0]
            payload = stream.read(size)
            if len(payload) != size:
                raise RuntimeError("Truncated IVF frame payload")
            count += 1
    return count


def verify_dovi_configuration(ffprobe, path):
    final_probe = ffprobe_json(
        ffprobe, path,
        ("-select_streams", "v:0", "-show_streams")
    )
    final_streams = final_probe.get("streams", [])
    side_data = final_streams[0].get("side_data_list", []) if final_streams else []
    dovi_config = next(
        (item for item in side_data
         if item.get("side_data_type") == "DOVI configuration record"),
        None
    )
    if not dovi_config:
        raise RuntimeError(
            "Final MKV is missing its Dolby Vision configuration record "
            "(MKVToolNix did not detect AV1 RPU metadata)"
        )
    expected_dovi = {
        "dv_profile": 10,
        "rpu_present_flag": 1,
        "el_present_flag": 0,
        "bl_present_flag": 1,
        "dv_bl_signal_compatibility_id": 1,
    }
    for key, expected in expected_dovi.items():
        if int(dovi_config.get(key, -1)) != expected:
            raise RuntimeError(
                "Final Dolby Vision configuration mismatch: {}={} (expected {})".format(
                    key, dovi_config.get(key), expected
                )
            )
    log("Verified final Dolby Vision Profile 10 BL+RPU configuration")


def remux_dovi_only(ffmpeg, ffprobe, output, frame_rate, workdir):
    encoded_ivf = os.path.join(workdir, "encoded_dovi.ivf")
    remuxed = os.path.join(
        workdir, "final_with_dovi" + (os.path.splitext(output)[1] or ".mkv")
    )

    output_info = get_video_info(ffprobe, output)
    if output_info.get("codec_name") != "av1":
        raise RuntimeError(
            "Dolby Vision remux requested, but output codec is '{}'".format(
                output_info.get("codec_name")
            )
        )
    if get_video_stream_count(ffprobe, output) != 1:
        raise RuntimeError(
            "Dolby Vision-only final remux currently requires exactly one video stream"
        )

    run_checked(
        [
            ffmpeg, "-hide_banner", "-y", "-i", output,
            "-map", "0:v:0", "-c:v", "copy",
            "-r", str(frame_rate), "-f", "ivf", encoded_ivf
        ],
        "Extracting encoded AV1 video for Dolby Vision container finalization"
    )
    frames = ivf_frame_count(encoded_ivf)
    if frames <= 0:
        raise RuntimeError("Encoded AV1 IVF contains no video frames")

    mkvmerge = require_mkvmerge()
    run_checked(
        [
            mkvmerge, "--output", remuxed,
            "--no-video", output,
            "--default-duration",
            "0:{}/{}fps".format(frame_rate.numerator, frame_rate.denominator),
            encoded_ivf,
        ],
        "Remuxing Dolby Vision-only AV1 with MKVToolNix to rebuild Matroska dvvC"
    )

    verify_dovi_configuration(ffprobe, remuxed)
    os.replace(remuxed, output)
    log("Dolby Vision-only AV1 remux completed ({} frames)".format(frames))


def inject_hdr10plus(ffmpeg, ffprobe, hdr_tool, output, json_path, json_frames, workdir,
                     dovi_present=False, frame_rate=None):
    encoded_ivf = os.path.join(workdir, "encoded.ivf")
    injected_ivf = os.path.join(workdir, "encoded_hdr10plus.ivf")
    verified_json = os.path.join(workdir, "verified_hdr10plus.json")

    output_info = get_video_info(ffprobe, output)
    if output_info.get("codec_name") != "av1":
        raise RuntimeError(
            "HDR10+ AV1 injection requested, but output codec is '{}'".format(
                output_info.get("codec_name")
            )
        )

    run_checked(
        [
            ffmpeg, "-hide_banner", "-y", "-i", output,
            "-map", "0:v:0", "-c:v", "copy"
        ] + (["-r", str(frame_rate)] if dovi_present else []) + [
            "-f", "ivf", encoded_ivf
        ],
        "Extracting encoded AV1 video for HDR10+ injection"
    )

    video_frames = ivf_frame_count(encoded_ivf)
    if video_frames != json_frames:
        raise RuntimeError(
            "Frame count mismatch: AV1 has {} frames, source HDR10+ has {}. "
            "Refusing to duplicate or truncate dynamic metadata.".format(video_frames, json_frames)
        )

    run_checked(
        [
            hdr_tool, "inject", "-i", encoded_ivf,
            "-j", json_path, "-o", injected_ivf
        ],
        "Injecting HDR10+ T.35 metadata into AV1 OBUs"
    )

    if ivf_frame_count(injected_ivf) != video_frames:
        raise RuntimeError("AV1 HDR10+ injector changed the encoded frame count")

    run_checked(
        [
            hdr_tool, "extract", injected_ivf, "-o", verified_json
        ],
        "Verifying HDR10+ metadata in injected AV1"
    )
    with open(verified_json, "r", encoding="utf-8") as stream:
        verified = json.load(stream)
    verified_scenes = verified.get("SceneInfo")
    if not isinstance(verified_scenes, list) or len(verified_scenes) != video_frames:
        raise RuntimeError("HDR10+ verification failed: extracted metadata frame count differs")

    extension = os.path.splitext(output)[1] or ".mkv"
    remuxed = os.path.join(workdir, "final_with_hdr10plus" + extension)

    if dovi_present:
        # FFmpeg's IVF demuxer drops the Matroska DOVI configuration record.
        # Its dovi_rpu BSF cannot reliably reconstruct AV1 Profile 10 signaling
        # from an IVF stream. MKVToolNix parses the AV1 Dolby Vision T.35/EMDF
        # metadata OBU and writes the corresponding Matroska dvvC mapping.
        mkvmerge = tool("mkvmerge", "MKVMERGE")
        run_checked(
            [
                mkvmerge, "--output", remuxed,
                "--no-video", output,
                "--default-duration",
                "0:{}/{}fps".format(frame_rate.numerator, frame_rate.denominator),
                injected_ivf,
            ],
            "Muxing AV1 with MKVToolNix and rebuilding Dolby Vision dvvC from AV1 RPU"
        )
    else:
        remux_command = [
            ffmpeg, "-hide_banner", "-y",
            "-i", output, "-i", injected_ivf,
            "-map", "0", "-map", "-0:v:0", "-map", "1:v:0",
            "-c", "copy", "-map_metadata", "0", "-map_chapters", "0",
            remuxed,
        ]
        run_checked(
            remux_command,
            "Remuxing AV1 with injected HDR10+ metadata"
        )

    if dovi_present:
        verify_dovi_configuration(ffprobe, remuxed)

    final_json = os.path.join(workdir, "final_verified_hdr10plus.json")
    run_checked(
        [hdr_tool, "extract", remuxed, "-o", final_json],
        "Verifying HDR10+ metadata after final MKV remux"
    )
    with open(final_json, "r", encoding="utf-8") as stream:
        final_metadata = json.load(stream)
    final_scenes = final_metadata.get("SceneInfo")
    if not isinstance(final_scenes, list) or len(final_scenes) != video_frames:
        raise RuntimeError(
            "Final MKV HDR10+ verification failed: expected {} frames, found {}".format(
                video_frames, len(final_scenes) if isinstance(final_scenes, list) else 0
            )
        )

    # Publish the remuxed file only after all metadata checks have passed.
    os.replace(remuxed, output)


def main(config_path):
    with open(config_path, "r", encoding="utf-8") as stream:
        config = json.load(stream)
    try:
        os.unlink(config_path)
    except OSError:
        pass

    source = config["source"]
    output = config["output"]
    ffmpeg_args = config["ffmpeg_args"]
    crop_value = config.get("crop_value")
    dovi_present = bool(config.get("dovi_rpu_present"))
    hdr10plus_present = bool(config.get("hdr10plus_present"))

    ffmpeg = tool("ffmpeg")
    ffprobe = tool("ffprobe")
    if dovi_present:
        require_mkvmerge()
    workdir = tempfile.mkdtemp(prefix=".unmanic-av1-metadata-", dir=os.path.dirname(output) or None)

    try:
        info = get_video_info(ffprobe, source)
        source_frame_rate = (
            get_constant_frame_rate(info) if dovi_present else None
        )
        if info.get("codec_name") != "hevc" and dovi_present and crop_value:
            raise RuntimeError("The crop-adjusted DV path currently requires an HEVC source")
        if dovi_present and crop_value and get_video_stream_count(ffprobe, source) != 1:
            raise RuntimeError(
                "Crop-adjusted DV preparation currently requires exactly one video stream"
            )
        if dovi_present and get_video_stream_count(ffprobe, source) != 1:
            raise RuntimeError(
                "Dolby Vision final remux currently requires exactly one video stream"
            )

        hdr_tool = None
        hdr_json = None
        hdr_frames = None
        if hdr10plus_present:
            hdr_tool = tool("hdr10plus_tool_av1", "HDR10PLUS_TOOL_AV1")
            hdr_json = os.path.join(workdir, "source_hdr10plus.json")
            hdr_frames = extract_hdr10plus(hdr_tool, source, hdr_json)

        if dovi_present and crop_value:
            dovi_tool = tool("dovi_tool", "DOVI_TOOL")
            frame_rate = info.get("avg_frame_rate") or info.get("r_frame_rate")
            nominal_rate = info.get("r_frame_rate")
            if not frame_rate or frame_rate in ("0/0", "N/A"):
                raise RuntimeError("Could not determine source frame rate for prepared HEVC")
            if nominal_rate and nominal_rate not in ("0/0", "N/A"):
                if Fraction(frame_rate) != Fraction(nominal_rate):
                    raise RuntimeError(
                        "Variable-frame-rate source detected; refusing to remap timestamps "
                        "from the temporary HEVC elementary stream"
                    )
            prepared_hevc, resolved_crop = prepare_dovi_hevc(
                ffmpeg, dovi_tool, source, workdir, crop_value, info
            )
            ffmpeg_args = apply_resolved_crop(ffmpeg_args, crop_value, resolved_crop)
            prepared_frames = count_video_packets(ffprobe, prepared_hevc, "hevc")
            source_frames = count_video_packets(ffprobe, source)
            if prepared_frames != source_frames:
                raise RuntimeError(
                    "Prepared HEVC frame count {} differs from source {}".format(
                        prepared_frames, source_frames
                    )
                )
            ffmpeg_args = add_prepared_hevc_input(
                ffmpeg_args, source, prepared_hevc, frame_rate, info.get("start_time")
            )

        # The outer Unmanic runner parses stdout only. FFmpeg's default
        # human-readable stats are written to stderr, so explicitly enable its
        # machine-readable progress protocol on stdout for the worker parser.
        ffmpeg_args = list(ffmpeg_args)
        if "-progress" in ffmpeg_args:
            progress_index = ffmpeg_args.index("-progress")
            if progress_index + 1 >= len(ffmpeg_args):
                raise RuntimeError("FFmpeg -progress option is missing its output URL")
            ffmpeg_args[progress_index + 1] = "pipe:1"
        else:
            ffmpeg_args[-1:-1] = ["-progress", "pipe:1"]

        run_encode(ffmpeg, ffmpeg_args)

        if hdr10plus_present:
            inject_hdr10plus(
                ffmpeg, ffprobe, hdr_tool, output, hdr_json, hdr_frames, workdir,
                dovi_present=dovi_present, frame_rate=source_frame_rate
            )
        elif dovi_present:
            remux_dovi_only(
                ffmpeg, ffprobe, output, source_frame_rate, workdir
            )
        log("AV1 metadata pipeline completed successfully")
        # Distinguish successful completion of the full staged pipeline from
        # FFmpeg's earlier progress=end marker.
        print("pipeline=end", flush=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: av1_hdr_pipeline.py CONFIG.json", file=sys.stderr)
        raise SystemExit(2)
    try:
        main(sys.argv[1])
    except Exception as exc:
        log("ERROR: {}".format(exc))
        raise SystemExit(1)
