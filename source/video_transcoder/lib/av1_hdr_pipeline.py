#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metadata-aware AV1 worker wrapper for Unmanic.

The wrapper keeps the plugin's generated FFmpeg command intact, with two
surgical additions:
  * when a DV P8.1 source is cropped, inject a crop-adjusted RPU into a
    temporary HEVC elementary stream and use that stream for video decoding;
  * when HDR10+ is present, inject its per-frame T.35 metadata into the
    encoded AV1 IVF stream before remuxing the final container.

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
         "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames,nb_read_frames")
    )
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError("No video stream found in source")
    return streams[0]


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


def validate_crop_against_rpu(dovi_tool, source, crop_value, source_info):
    try:
        crop_w, crop_h, crop_x, crop_y = [int(value) for value in crop_value.split(":")]
    except (AttributeError, TypeError, ValueError):
        raise RuntimeError("Invalid detected crop value: {}".format(crop_value))

    source_w = int(source_info.get("width") or 0)
    source_h = int(source_info.get("height") or 0)
    crop_right = source_w - crop_w - crop_x
    crop_bottom = source_h - crop_h - crop_y

    # The currently validated RPU edit clears top/bottom letterbox offsets.
    # Do not apply it to side crops, asymmetric crops, or unknown geometry.
    if crop_w != source_w or crop_x != 0 or crop_y != crop_bottom:
        raise RuntimeError(
            "The tested DV RPU crop edit only supports symmetric top/bottom letterbox crops; "
            "detected crop is {}".format(crop_value)
        )

    summary = run_checked(
        [dovi_tool, "info", "-s", source],
        "Checking Dolby Vision Level 5 active-area offsets",
        capture=True
    )
    match = re.search(
        r"L5 offsets:\s*top=(\d+)(?:\.\.(\d+))?,\s*bottom=(\d+)(?:\.\.(\d+))?",
        summary,
        flags=re.IGNORECASE
    )
    if match:
        top_max = int(match.group(2) or match.group(1))
        bottom_max = int(match.group(4) or match.group(3))
        if top_max != crop_y or bottom_max != crop_bottom:
            raise RuntimeError(
                "Detected crop ({}, {}) does not match DV L5 offsets (top={}, bottom={}); "
                "refusing to rewrite RPU".format(crop_y, crop_bottom, top_max, bottom_max)
            )
    else:
        log("No summarized L5 offsets found; applying the tested active-area reset")

def prepare_dovi_hevc(ffmpeg, dovi_tool, source, workdir, crop_value, source_info):
    validate_crop_against_rpu(dovi_tool, source, crop_value, source_info)
    source_hevc = os.path.join(workdir, "source.hevc")
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
        [dovi_tool, "-c", "extract-rpu", "-i", source, "-o", edited_rpu],
        "Extracting and clearing Dolby Vision Level 5 active-area offsets"
    )
    run_checked(
        [
            dovi_tool, "inject-rpu", "-i", source_hevc,
            "--rpu-in", edited_rpu, "-o", prepared_hevc
        ],
        "Injecting crop-adjusted Dolby Vision RPU into HEVC"
    )
    return prepared_hevc


def add_prepared_hevc_input(ffmpeg_args, source, prepared_hevc, frame_rate):
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
    args[source_input + 2:source_input + 2] = [
        "-r", frame_rate, "-f", "hevc", "-i", prepared_hevc
    ]

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


def inject_hdr10plus(ffmpeg, ffprobe, hdr_tool, output, json_path, json_frames, workdir):
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
            "-map", "0:v:0", "-c:v", "copy", "-f", "ivf", encoded_ivf
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
    run_checked(
        [
            ffmpeg, "-hide_banner", "-y",
            "-i", output, "-i", injected_ivf,
            "-map", "0", "-map", "-0:v:0", "-map", "1:v:0",
            "-c", "copy", "-map_metadata", "0", "-map_chapters", "0",
            remuxed
        ],
        "Remuxing AV1 with HDR10+ while preserving the other output streams"
    )
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
    workdir = tempfile.mkdtemp(prefix=".unmanic-av1-metadata-", dir=os.path.dirname(output) or None)

    try:
        info = get_video_info(ffprobe, source)
        if info.get("codec_name") != "hevc" and dovi_present and crop_value:
            raise RuntimeError("The crop-adjusted DV path currently requires an HEVC source")

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
            prepared_hevc = prepare_dovi_hevc(ffmpeg, dovi_tool, source, workdir, crop_value, info)
            prepared_frames = count_video_packets(ffprobe, prepared_hevc, "hevc")
            source_frames = count_video_packets(ffprobe, source)
            if prepared_frames != source_frames:
                raise RuntimeError(
                    "Prepared HEVC frame count {} differs from source {}".format(
                        prepared_frames, source_frames
                    )
                )
            ffmpeg_args = add_prepared_hevc_input(
                ffmpeg_args, source, prepared_hevc, frame_rate
            )

        run_encode(ffmpeg, ffmpeg_args)

        if hdr10plus_present:
            inject_hdr10plus(
                ffmpeg, ffprobe, hdr_tool, output, hdr_json, hdr_frames, workdir
            )
        log("AV1 metadata pipeline completed successfully")
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
