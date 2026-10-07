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
