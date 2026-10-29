# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: conformance.py
# Folder: gp2040ce_hostlighting/tools
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""The hlp-conformance and hlp-bench commands, built on gp2040ce_hostlighting.conformance.

hlp-conformance checks one board against the protocol and reports each rule it checks: the HELLO
capabilities, framing, every page and field mask, the pages' cross-references, takeover and keepalive timing,
the claim lease and generation, shared and exclusive claims, unsaved brightness, staging, events and
board-feature argument checks, delivery of replies to a second handle, then round trip and throughput.
hlp-bench runs the round trip and frame-rate measurements on their own, at length. Both hold the claim while
they run, so other applications leave the lights alone, and give it up at the end.
"""
import contextlib
import json
import statistics
import sys
import time

from gp2040ce_hostlighting.claim import LightsClaim
from gp2040ce_hostlighting.conformance import (CheckResult, Checks, _reports, measure_paced_frames, measure_round_trips,
                                               measure_unthrottled_frames, run_checks)
from gp2040ce_hostlighting.protocol import PAGE_SUMMARY, HostLightingClaimedError, HostLightingDevice, open_device
from gp2040ce_hostlighting.records import Settings, jsonable
from gp2040ce_hostlighting.tools.commands import add_claim_arguments, device_parser, hlp_command, ranged, take_claim


def _line(result: CheckResult) -> str:
    """Show one result as the report prints it."""
    if result.passed is None:
        return f"        {result.what}"
    if result.passed:
        return f"  PASS  {result.what}"
    return f"  FAIL  {result.what}" + (f"  ({result.detail})" if result.detail else '')


@hlp_command
def conformance() -> None:
    """Check a board's Host Lighting implementation against the protocol."""
    parser = device_parser("Check a GP2040-CE board's Host Lighting implementation against the protocol: HELLO, "
                           "framing, the pages and their cross-references, takeover, keepalive and claim timing, "
                           "the claim generation, shared and exclusive claims, unsaved brightness, staging, events, "
                           "argument checks and delivery of replies to a second handle, then round trip and "
                           "throughput. It holds the claim while it runs, so run it with no other lighting "
                           "application.")
    parser.add_argument('--settings', action='store_true',
                        help="also change and restore a keyboard code (when the board's settings allow it), "
                             "brightness, animation and speeds (writes the settings flash)")
    parser.add_argument('--quick', action='store_true', help="shorter timing runs")
    parser.add_argument('--json', action='store_true',
                        help="print the results as JSON when the run ends, the usual lines going to stderr")
    add_claim_arguments(parser)
    args = parser.parse_args()
    device = open_device(args.board_id)
    checks = Checks()
    start, section, board_id = time.monotonic(), '', ''
    out = sys.stdout
    with contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext():
        try:
            board_id = device.hello().board_id
            with take_claim(device, 'hlp-conformance', args.exclusive, args.force) as claim:
                for result in run_checks(device, claim, args.settings, args.quick,
                                         lambda: open_device(args.board_id)):
                    if result.section != section:
                        section = result.section
                        print(f"{section}:")
                    print(_line(checks.add(result)))
        finally:
            device.close()
        elapsed = time.monotonic() - start
        print(f"elapsed: {elapsed:.1f} s")
        print(f"RESULT: {'FAIL' if checks.failed else 'ALL PASS'} ({checks.passed} passed, {checks.failed} failed)")
    if args.json:
        print(json.dumps({'board_id': board_id, 'settings': args.settings, 'quick': args.quick,
                          'exclusive': args.exclusive, 'passed': checks.passed, 'failed': checks.failed,
                          'elapsed_seconds': round(elapsed, 1), 'results': jsonable(checks.results)}, indent=1),
              file=out)
    if checks.failed:
        raise SystemExit(1)


def _bench(device: HostLightingDevice, claim: LightsClaim, leds: int, quick: bool) -> None:
    """Run hlp-bench's measurements, maintaining the claim between them."""
    count = 100 if quick else 300
    print(f"round trip: {count} HELLO requests")
    trips = sorted(measure_round_trips(device, count, claim))
    claim.maintain()
    if trips:
        print(f"round trip: median {statistics.median(trips):.2f} ms, p95 {trips[int(len(trips) * 0.95) - 1]:.2f} ms "
              f"({len(trips)} of {count} answered)")
    frames = 60 if quick else 300
    print(f"60 fps: {frames} frames ({leds} LEDs, {_reports(leds)} reports per frame)")
    acknowledged = measure_paced_frames(device, claim, leds, frames)
    print(f"60 fps: {acknowledged} of {frames} frames acknowledged ({leds} LEDs, {_reports(leds)} reports per frame)")
    seconds = 1.0 if quick else 3.0
    for pipelined, name in ((False, 'every report answered'), (True, 'pipelined')):
        print(f"unthrottled, {name}: frames back to back for {seconds} s")
        rate = measure_unthrottled_frames(device, claim, leds, seconds, pipelined)
        print(f"unthrottled, {name}: {rate:.0f} frames/s")


@hlp_command
def bench() -> None:
    """Measure a board's Host Lighting round trip and frame rates."""
    parser = device_parser("Measure a GP2040-CE board's Host Lighting round trip and frame rates: HELLO round "
                           "trips, whole frames paced at 60 fps, then frames back to back with every report "
                           "answered and pipelined (only the last report answered).")
    parser.add_argument('--leds', type=ranged(1, 0xFFFF), help="LEDs per frame (default: the LED extent)")
    parser.add_argument('--quick', action='store_true', help="fewer samples: 100 round trips, 60 frames, 1 s runs")
    add_claim_arguments(parser)
    args = parser.parse_args()
    device = open_device(args.board_id)
    try:
        summary = device.read_record(PAGE_SUMMARY).records[0].summary
        if args.leds and args.leds > summary.led_limit:
            parser.error(f"--leds is at most the board's addressable LED limit, {summary.led_limit}")
        leds = args.leds or summary.led_extent or 1
        with take_claim(device, 'hlp-bench', args.exclusive, args.force, Settings(brightness_applied=False)) as claim:
            try:
                _bench(device, claim, leds, args.quick)
            except HostLightingClaimedError:
                pass            # the claim printed who took the lights over
    finally:
        device.close()
