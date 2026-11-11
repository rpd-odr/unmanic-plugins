#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    dolby_vision_detect.py

    Detect Dolby Vision RPU metadata in an FFprobe result.

    The detector is deliberately lightweight and does not inspect video frames.
    FFprobe exposes Dolby Vision information as stream side-data and, depending
    on the FFmpeg build/container, may expose the RPU/profile fields directly.
"""

import logging
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

    def detect_hdr10plus(self, probe_data) -> bool:
        """Detect HDR10+ SMPTE ST 2094-40 side data reported by FFprobe."""
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

        detected = walk(self._first_video_stream(probe_data))
        self._wlog("HDR10+ detection: {}".format("present" if detected else "not present"))
        return detected
