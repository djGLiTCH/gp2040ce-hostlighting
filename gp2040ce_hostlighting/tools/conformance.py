"""The hlp-conformance and hlp-bench commands, built on gp2040ce_hostlighting.conformance.

hlp-conformance checks one board against the protocol and reports each rule it checks: the HELLO
capabilities, framing, every page and field mask, the pages' cross-references, takeover and keepalive timing,
the claim lease and generation, shared and exclusive claims, unsaved brightness, staging, events and
board-feature argument checks, delivery of replies to a second handle, then round trip and throughput.
hlp-bench runs the round trip and frame-rate measurements on their own,
at length. Both hold the claim while they run,
so other applications leave the lights alone, and hand the lights back at the end.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import statistics
import time

from gp2040ce_hostlighting.conformance import (Checks, _Lease, _paced, _reports, _round_trips, _run_checks, _TakenOver,
                                               _unthrottled)
from gp2040ce_hostlighting.protocol import PAGE_SUMMARY, HostLightingDevice, HostLightingError, LightsClaim, open_device
from gp2040ce_hostlighting.tools.commands import add_claim_arguments, device_parser, hlp_command, holding_lights, ranged


@hlp_command
def conformance():
    """Check a board's Host Lighting implementation against the protocol."""
    parser = device_parser("Check a GP2040-CE board's Host Lighting implementation against the protocol: HELLO, "
                           "framing, the pages and their cross-references, takeover, keepalive and claim timing, "
                           "the claim generation, shared and exclusive claims, unsaved brightness, staging, events, "
                           "argument checks and delivery of replies to a second handle, "
                           "then round trip and throughput. It holds the "
                           "claim while it runs, so run it with no other lighting application.")
    parser.add_argument('--settings', action='store_true',
                        help="also change and restore brightness, animation and speeds (writes the settings flash)")
    parser.add_argument('--quick', action='store_true', help="shorter timing runs")
    add_claim_arguments(parser)
    args, _ = parser.parse_known_args()
    device = open_device(args.board_id)
    checks = Checks(report=print)
    start = time.monotonic()
    try:
        with holding_lights(device, 'hlp-conformance', args.force, args.exclusive) as claim:
            try:
                _run_checks(device, checks, _Lease(claim), args.settings, args.quick,
                            lambda: open_device(args.board_id))
            except (_TakenOver, HostLightingError, OSError) as error:
                checks.check(False, 'the checks ran to the end', str(error))
    finally:
        device.close()
    print(f"elapsed: {time.monotonic() - start:.1f} s")
    print(f"RESULT: {'FAIL' if checks.failed else 'ALL PASS'} ({checks.passed} passed, {checks.failed} failed)")
    if checks.failed:
        raise SystemExit(1)


def _keeper(claim: LightsClaim):
    """Build a function that renews the claim each second and raises _TakenOver once it is lost."""
    renewed = [time.monotonic()]

    def keep():
        if time.monotonic() - renewed[0] >= 1.0:
            renewed[0] = time.monotonic()
            if not claim.renew():
                raise _TakenOver()
    return keep


def _bench(device: HostLightingDevice, leds: int, quick: bool, keep) -> None:
    count = 100 if quick else 300
    trips = sorted(_round_trips(device, count))
    keep()
    if trips:
        print(f"round trip: median {statistics.median(trips):.2f} ms, p95 {trips[int(len(trips) * 0.95) - 1]:.2f} ms "
              f"({len(trips)} of {count} answered)")
    frames = 60 if quick else 300
    acknowledged = _paced(device, leds, frames, keep)
    print(f"60 fps: {acknowledged} of {frames} frames acknowledged ({leds} LEDs, {_reports(leds)} reports per frame)")
    seconds = 1.0 if quick else 3.0
    for pipelined, name in ((False, 'every report answered'), (True, 'pipelined')):
        print(f"unthrottled, {name}: {_unthrottled(device, leds, seconds, pipelined, keep):.0f} frames/s")


@hlp_command
def bench():
    """Measure a board's Host Lighting round trip and frame rates."""
    parser = device_parser("Measure a GP2040-CE board's Host Lighting round trip and frame rates: HELLO round "
                           "trips, whole frames paced at 60 fps, then frames back to back with every report "
                           "answered and pipelined (only the last report answered).")
    parser.add_argument('--leds', type=ranged(1, 0xFFFF), help="LEDs per frame (default: the LED extent)")
    parser.add_argument('--quick', action='store_true', help="fewer samples: 100 round trips, 60 frames, 1 s runs")
    add_claim_arguments(parser)
    args, _ = parser.parse_known_args()
    device = open_device(args.board_id)
    try:
        leds = args.leds or device.read_record(PAGE_SUMMARY).records[0].summary.led_extent or 1
        try:
            with holding_lights(device, 'hlp-bench', args.force, args.exclusive, brightness=False) as claim:
                _bench(device, leds, args.quick, _keeper(claim))
        except _TakenOver:
            pass
    finally:
        device.close()
