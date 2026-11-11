#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    dolby_vision_detect.py

    Detect Dolby Vision RPU metadata in an FFprobe result.

    The detector is deliberately lightweight and does not inspect video frames.
    FFprobe exposes Dolby Vision information as stream side-data and, depending
    on the FFmpeg build/container, may expose the RPU/profile fields directly.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any

from video_transcoder.lib.tools import append_worker_log


class DolbyVisionDetect:
    def __init__(self, worker_log=None, logger=None):
        self.worker_log = worker_log if isinstance(worker_log, list) else None
        self.logger = logger or logging.getLogger("Unmanic.Plugin.video_transcoder")

    def _wlog(self, line: str):
        append_worker_log(self.worker_log, line)

    def _contains_rpu(self, value: Any) -> bool:
        """Return True only for an explicit FFprobe indication of RPU data."""
        if isinstance(value, dict):
            for key, item in value.items():
                key_l = str(key).lower()
                if key_l == "rpu_present_flag":
                    try:
                        if int(item) == 1:
                            return True
                    except (TypeError, ValueError):
                        if str(item).lower() in ("true", "yes", "on"):
                            return True
                if self._contains_rpu(item):
                    return True

        elif isinstance(value, list):
            for item in value:
                if self._contains_rpu(item):
                    return True

        return False

    def detect_rpu(self, probe_data) -> bool:
        """
        Return True only when FFprobe explicitly reports RPU metadata.

        Do not infer RPU presence from dv_profile alone: a Dolby Vision profile
        field can exist without an RPU payload. Profile 8.x can legitimately
        have no enhancement layer while still carrying an RPU.
        """
        try:
            detected = self._contains_rpu(probe_data)
        except Exception as exc:
            self.logger.debug("Dolby Vision detection failed: %s", exc)
            detected = False

        self._wlog(
            "Dolby Vision detector: {}.".format(
                "RPU metadata found" if detected else "no RPU metadata found"
            )
        )
        return detected

    def _find_stream_values(self, value, keys):
        found = {}
        if isinstance(value, dict):
            for key, item in value.items():
                key_l = str(key).lower()
                if key_l in keys and key_l not in found:
                    found[key_l] = item
                found.update(self._find_stream_values(item, keys) if isinstance(item, (dict, list)) else {})
        elif isinstance(value, list):
            for item in value:
                found.update(self._find_stream_values(item, keys))
        return found

    def _first_video_stream(self, probe_data):
        if isinstance(probe_data, dict):
            streams = probe_data.get("streams")
            if isinstance(streams, list):
                for stream in streams:
                    if isinstance(stream, dict) and str(stream.get("codec_type", "")).lower() == "video":
                        return stream
        return probe_data

    def detect_supported_p8_1_rpu(self, probe_data) -> bool:
        """
        Only enable the tested Dolby Vision Profile 8.1 path.
        Profile 7.6 and other profiles are deliberately left untouched.
        """
        video_data = self._first_video_stream(probe_data)
        values = self._find_stream_values(
            video_data,
            {"dv_profile", "dv_bl_signal_compatibility_id"}
        )
        try:
            profile = int(values.get("dv_profile", -1))
            compatibility = int(values.get("dv_bl_signal_compatibility_id", -1))
        except (TypeError, ValueError):
            profile, compatibility = -1, -1

        has_rpu = self._contains_rpu(video_data)
        supported = has_rpu and profile == 8 and compatibility == 1
        self._wlog(
            "Dolby Vision support check: profile={}, BL compatibility={}, RPU={}, supported={}".format(
                profile, compatibility, has_rpu, supported
            )
        )
        return supported

    def detect_hdr10plus(self, probe_data, source_path=None) -> bool:
        """
        Detect HDR10+ from FFprobe side data, with a bitstream extractor fallback.

        Some FFprobe builds do not expose SMPTE ST 2094-40 side data for HEVC
        even when HDR10+ metadata is present in the bitstream. In that case,
        use hdr10plus_tool_av1 and require a non-empty SceneInfo array.
        """
        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).lower() in ("side_data_type", "side_data_type_name"):
                        label = str(item).lower()
                        if "2094-40" in label or "hdr10+" in label:
                            return True
                    if walk(item):
                        return True
            elif isinstance(value, list):
                return any(walk(item) for item in value)
            return False

        if walk(self._first_video_stream(probe_data)):
            self._wlog("HDR10+ detection: present (FFprobe side data)")
            return True

        if not source_path:
            self._wlog("HDR10+ detection: not reported by FFprobe; no source path for bitstream fallback")
            return False

        configured_tool = os.environ.get("HDR10PLUS_TOOL_AV1", "")
        candidates = [
            configured_tool,
            shutil.which("hdr10plus_tool_av1") or "",
            "/opt/unmanic-tools/hdr10plus_tool_av1",
        ]
        hdr_tool = next(
            (path for path in candidates if path and os.path.isfile(path) and os.access(path, os.X_OK)),
            None,
        )
        if not hdr_tool:
            self._wlog(
                "HDR10+ detection: FFprobe reported no metadata and hdr10plus_tool_av1 was not found"
            )
            return False

        try:
            with tempfile.TemporaryDirectory(prefix="unmanic-hdr10plus-detect-") as temp_dir:
                json_path = os.path.join(temp_dir, "source_hdr10plus.json")
                result = subprocess.run(
                    [hdr_tool, "extract", source_path, "-o", json_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=180,
                )
                if result.returncode != 0:
                    detail = (result.stdout or "").strip().replace("\n", " ")
                    self._wlog(
                        "HDR10+ bitstream detection failed (exit {}): {}".format(
                            result.returncode, detail[-500:]
                        )
                    )
                    return False

                with open(json_path, "r", encoding="utf-8") as metadata_file:
                    metadata = json.load(metadata_file)
                scenes = metadata.get("SceneInfo")
                frame_count = len(scenes) if isinstance(scenes, list) else 0
                detected = frame_count > 0
                self._wlog(
                    "HDR10+ detection: {} (hdr10plus_tool_av1, {} metadata frames)".format(
                        "present" if detected else "not present",
                        frame_count,
                    )
                )
                if detected:
                    # Keep the same executable path for the staged pipeline,
                    # which resolves this variable when it starts.
                    os.environ["HDR10PLUS_TOOL_AV1"] = hdr_tool
                return detected
        except subprocess.TimeoutExpired:
            self._wlog("HDR10+ bitstream detection timed out after 180 seconds")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._wlog("HDR10+ bitstream detection failed: {}".format(exc))
        return False
