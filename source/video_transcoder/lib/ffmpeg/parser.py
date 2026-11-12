#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    plugins.parser.py

    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     12 Aug 2021, (9:00 AM)

    Copyright:
        Copyright (C) 2021 Josh Sunnex

        This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
        Public License as published by the Free Software Foundation, version 3.

        This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
        implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License
        for more details.

        You should have received a copy of the GNU General Public License along with this program.
        If not, see <https://www.gnu.org/licenses/>.

"""
import datetime
import math
import re
from logging import Logger

from .probe import Probe


class Parser(object):
    """
    Parser

    Class to manage parsing the FFmpeg commandline output and return progress data for Unmanic.
    """

    def __init__(self, logger: Logger, duration=None, total_frames=None, require_explicit_end=False):
        self.logger = logger
        self.percent = '0'
        self.time = 0.0
        self.frame = '0'
        self.speed = '0'
        self.bitrate = '0'
        self.src_fps = None
        self.duration = None
        self.total_frames = None
        self.require_explicit_end = require_explicit_end
        self._progress_debug_events = 0
        self._progress_debug_high_logged = False

    def set_probe(self, probe: Probe):
        """Set source video FPS and duration used to calculate progress."""
        self.src_fps = None
        streams = probe.get('streams', [])
        video_stream = next((s for s in streams if s.get('codec_type') == 'video'), None)
        if video_stream is None and streams:
            video_stream = streams[0]
        try:
            rate = video_stream.get('avg_frame_rate') if video_stream else None
            if rate and rate not in ('0/0', 'N/A'):
                numerator, denominator = rate.split('/', 1)
                denominator = float(denominator)
                if denominator:
                    self.src_fps = float(numerator) / denominator
        except (AttributeError, TypeError, ValueError, ZeroDivisionError):
            self.logger.warning('Cannot use input FPS for FFmpeg conversion progress')

        try:
            self.duration = float(probe.get('format', {}).get('duration'))
            if self.duration <= 0:
                self.duration = None
        except (TypeError, ValueError):
            self.duration = None
            self.logger.warning('Cannot use input Duration for FFmpeg conversion progress')

        if self.duration and self.src_fps:
            self.total_frames = int(self.duration * self.src_fps)

        if self.src_fps is None and self.duration is None:
            raise ValueError('Unable to match against FPS or Duration.')

        format_info = probe.get('format', {})
        self.logger.warning(
            '[AV1 progress debug] Probe: file=%r duration_raw=%r duration_seconds=%r '
            'video_codec=%r avg_frame_rate=%r r_frame_rate=%r fps_used=%r total_frames=%r '
            'explicit_end=%r',
            format_info.get('filename'),
            format_info.get('duration'),
            self.duration,
            video_stream.get('codec_name') if video_stream else None,
            video_stream.get('avg_frame_rate') if video_stream else None,
            video_stream.get('r_frame_rate') if video_stream else None,
            self.src_fps,
            self.total_frames,
            self.require_explicit_end,
        )

    def parse_progress(self, line_text):
        """Parse FFmpeg stats or -progress key/value output."""
        if not line_text:
            return {'percent': self.percent}

        line = str(line_text).strip()
        if line == 'progress=end':
            # FFmpeg has finished encoding, but the staged HDR pipeline still
            # has metadata injection/remux/verification work to complete.
            if self.require_explicit_end:
                self.percent = str(min(99, int(self.percent)))
            else:
                self.percent = '100'
            self.logger.warning(
                '[AV1 progress debug] FFmpeg progress=end: frame=%s out_time=%.6f duration=%r returned_percent=%s',
                self.frame, self.time, self.duration, self.percent
            )
            return {'percent': self.percent}

        if line == 'pipeline=end':
            self.percent = '100'
            self.logger.warning(
                '[AV1 progress debug] staged pipeline completed: frame=%s out_time=%.6f duration=%r returned_percent=100',
                self.frame, self.time, self.duration
            )
            return {'percent': self.percent}

        if line.startswith('frame='):
            value = line.split('=', 1)[1].strip()
            if value.isdigit() and int(value) > int(self.frame):
                self.frame = value

        # FFmpeg's machine-readable protocol uses out_time= rather than time=.
        if line.startswith('out_time='):
            timestamp = line.split('=', 1)[1].strip()
            try:
                self.time = self.time_string_to_seconds(timestamp)
            except (TypeError, ValueError):
                pass
        elif 'time=' in line and not self.require_explicit_end:
            # Staged HDR jobs run helper FFmpeg processes before and after the
            # main encode. Their human-readable stderr stats are inherited by
            # the wrapper and merged into Unmanic's stdout. Never treat those
            # helper-process time= values as encode progress: staged jobs must
            # use only the main encoder's machine-readable out_time= events.
            timestamp = line.split('time=', 1)[1].split()[0]
            try:
                self.time = self.time_string_to_seconds(timestamp)
            except (TypeError, ValueError):
                pass

        if line.startswith('speed='):
            self.speed = line.split('=', 1)[1].strip()
        elif 'speed=' in line:
            self.speed = line.split('speed=', 1)[1].split()[0]

        percent = None
        if self.duration and self.time > 0:
            percent = (self.time / self.duration) * 100.0
        elif self.total_frames and int(self.frame) > 0:
            percent = (int(self.frame) / self.total_frames) * 100.0

        raw_percent = percent
        if percent is not None:
            # Keep staged HDR jobs below 100 until metadata injection and
            # final-container verification have completed.
            maximum = 99 if self.require_explicit_end else 100
            value = max(0, min(maximum, int(percent)))
            if value > int(self.percent):
                self.percent = str(value)

        if line.startswith('out_time=') and (
            self._progress_debug_events < 12 or
            (raw_percent is not None and raw_percent >= 90 and not self._progress_debug_high_logged)
        ):
            self._progress_debug_events += 1
            if raw_percent is not None and raw_percent >= 90:
                self._progress_debug_high_logged = True
            self.logger.warning(
                '[AV1 progress debug] event=%d frame=%s out_time=%.6f duration=%r '
                'raw_percent=%r returned_percent=%s',
                self._progress_debug_events,
                self.frame,
                self.time,
                self.duration,
                raw_percent,
                self.percent,
            )

        return {'percent': self.percent}

    @staticmethod
    def time_string_to_seconds(time_string):
        """Convert FFmpeg HH:MM:SS.fraction timestamps to seconds."""
        hours, minutes, seconds = time_string.split(':', 2)
        return (int(hours) * 3600) + (int(minutes) * 60) + float(seconds)

    @staticmethod
    def get_progress_from_regex_of_string(line, regex_string, default=None):
        """
        Parse value from line text using the given regular expression.
        If no match is found, return the given default value.

        :param line:
        :param regex_string:
        :param default:
        :return:
        """
        if default is None:
            default = 0

        return_value = default
        regex = re.compile(regex_string)
        findall = re.findall(regex, line)
        if findall:
            split_list = findall[-1]
            if len(split_list) == 2:
                return_value = split_list[1].strip()
        return return_value
