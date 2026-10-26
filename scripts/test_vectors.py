"""Generate the HLP 2.0 test vectors from the library's ModelBoard, or replay them on a board.

Each scenario starts from an unclaimed board with the CONFIGURE defaults and sends exact requests. On the model every
request is recorded, then every report the board sent after it, replies and events, as hex. On a board the same
scenarios run, and each report is compared with the model's by what the board's own state cannot change
(test-vectors/README.md).

  python scripts/test_vectors.py               write test-vectors/vectors.json from the model
  python scripts/test_vectors.py --board ID    replay the vectors on the board with this ID and print the comparison

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import json
import os
import struct
import sys
import time
from typing import Any, Callable, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from gp2040ce_hostlighting import protocol as hl  # noqa: E402
from gp2040ce_hostlighting.testing import ModelBoard, summary_record  # noqa: E402

VECTORS = os.path.join(ROOT, 'test-vectors', 'vectors.json')
A, B = 0x48A1A1A1, 0x48B2B2B2
NAME_A, NAME_B = b'vec-a', b'vec-b'
RECORDS = 12                    # a page reply's, and an event's, first record
# page 1's groups: State, then Controller (its holder token and name, flags at +20, generation at +21), then
# Settings
STATE_SIZE, CONTROLLER_SIZE, SETTINGS_SIZE = hl.PAGE_GROUPS[hl.PAGE_STATE]
HOLDER = RECORDS + STATE_SIZE
SETUP_SECOND_ANIMATION = 'second_animation'  # setup: a second animation on page 13 (test-vectors/README.md)
BOARD_IDLE_TIMEOUT = 'idle_timeout'          # board_before: the animations' idle timeout fires
FILL_RED = bytes([hl.PIXEL_RGB << 4]) + bytes([8, 0, 0])


class Driver:
    """Send exact requests and keep what came back."""

    def __init__(self) -> None:
        """Start with no steps recorded."""
        self.steps: list[dict[str, Any]] = []
        self.seq = 0
        self.recording = True
        self.wait_before = 0.0
        self.board_before: Optional[str] = None

    def write(self, report: bytes) -> None:
        """Send one request report."""
        raise NotImplementedError

    def collect(self, expect: bool, settle: float) -> list[bytes]:
        """Read every report the board sent after a request."""
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        """Let time pass on the board's clock."""
        raise NotImplementedError

    def set_idle(self) -> None:
        """Have the animations' idle timeout turn them off."""
        raise NotImplementedError

    def wait(self, seconds: float) -> None:
        """Let time pass, recorded as the next step's wait_before."""
        self.wait_before += seconds
        self.sleep(seconds)

    def idle(self) -> None:
        """Have the animations go idle, recorded as the next step's board_before."""
        self.board_before = BOARD_IDLE_TIMEOUT
        self.set_idle()

    def record(self, label: str, report: bytes, read: bool, got: list[bytes]) -> None:
        """Record a step with what came before it, then clear that."""
        if self.recording:
            self.steps.append({'label': label, 'wait_before': round(self.wait_before, 3),
                               'board_before': self.board_before, 'request': report.hex(), 'read': read,
                               'reports': [r.hex() for r in got]})
        self.wait_before, self.board_before = 0.0, None

    def send(self, label: str, command: int, payload: bytes = b'', flags: int = 0) -> None:
        """Send a request and read nothing: its reports come in a later step."""
        self.seq = self.seq % 255 + 1
        report = hl.build_request(command, self.seq, payload, flags)
        self.write(report)
        self.record(label, report, False, [])

    def req(self, label: str, command: int, payload: bytes = b'', flags: int = 0, settle: float = 0.03) -> bytes:
        """Send a request, record it and what followed it, and return its reply, or b'' when none came."""
        self.seq = self.seq % 255 + 1
        report = hl.build_request(command, self.seq, payload, flags)
        self.write(report)
        got = self.collect(expect=not flags & hl.FLAG_NO_REPLY, settle=settle)
        self.record(label, report, True, got)
        replies = [r for r in got if r[0] == command | hl.REPLY_FLAG and r[1] == self.seq]
        return replies[0] if replies else b''

    def page1(self, label: str = 'page 1') -> bytes:
        """Read page 1 whole, without a token."""
        return self.req(label, hl.CMD_GET_PAGE, struct.pack('<BHIH', hl.PAGE_STATE, 0, 0, 0))


class Model(Driver):
    """The library's ModelBoard, read without waiting."""

    def __init__(self, animations: int = 1) -> None:
        """Make a model board, with a second animation when asked."""
        super().__init__()
        self.board = ModelBoard()
        if animations == 2:
            self.board.animations.append([list(group) for group in self.board.animations[0]][0:1] +
                                         self.board.animations[0][1:])
            self.board.summary = summary_record(led_extent=4, grid_width=2, grid_height=2, profiles=1, controls=3,
                                                lit_controls=1, unlit_controls=2, lights=2, animations=2)
            self.board._refresh()

    def write(self, report: bytes) -> None:
        """Hand the request to the model."""
        self.board.write(b'\x00' + report)

    def collect(self, expect: bool, settle: float) -> list[bytes]:
        """Take every report the model has queued."""
        out = []
        while True:
            report = self.board.read(hl.REPORT_SIZE, 0)
            if not report:
                return out
            out.append(bytes(report))

    def sleep(self, seconds: float) -> None:
        """Advance the model's clock."""
        self.board.sleep(seconds)

    def set_idle(self) -> None:
        """Set the model's idle flag."""
        self.board.idle(True)


class Board(Driver):
    """A board on the bus, found by its board ID."""

    def __init__(self, board_id: str) -> None:
        """Open the board, or exit when it is not on the bus."""
        super().__init__()
        device = hl.find_board(board_id.lower())
        if device is None:
            raise SystemExit(f'board {board_id} not found')
        self.device = device
        self.device.handle.set_nonblocking(False)

    def write(self, report: bytes) -> None:
        """Write the request report."""
        self.device._write(b'\x00' + report)

    def collect(self, expect: bool, settle: float) -> list[bytes]:
        """Read reports until none has come for settle seconds, waiting longer for an expected reply."""
        out = []
        deadline = time.perf_counter() + (0.25 if expect else settle)
        while time.perf_counter() < deadline:
            report = self.device._read(10)
            if report:
                out.append(report)
                deadline = min(deadline, time.perf_counter() + settle)
        return out

    def sleep(self, seconds: float) -> None:
        """Sleep."""
        time.sleep(seconds)

    def set_idle(self) -> None:
        """Refuse: a board goes idle only by its own timeout."""
        raise NotImplementedError('a board goes idle only by its own timeout')


def claim(d: Driver, label: str, action: int, token: int, name: bytes, flags: int = 0, cflags: int = 0) -> bytes:
    """Send CLAIM."""
    return d.req(label, hl.CMD_CLAIM, bytes([action]) + struct.pack('<I', token) + name.ljust(16, b'\x00') +
                 bytes([cflags]), flags)


def gen_of(claim_reply: bytes) -> int:
    """Give the claim generation a CLAIM reply shows."""
    return claim_reply[29]


def as_holder(generation: int, exclusive: bool = False) -> int:
    """Give the request flags that carry a generation, with HOLDER under an exclusive claim."""
    return generation << hl.FLAG_GENERATION_SHIFT | (hl.FLAG_HOLDER if exclusive else 0)


def configure(d: Driver, label: str, flags: int, takeover: int = 0, keepalive_ms: int = 2000,
              brightness: int = 1) -> bytes:
    """Send CONFIGURE."""
    return d.req(label, hl.CMD_CONFIGURE, struct.pack('<BHB', takeover, keepalive_ms, brightness), flags)


def led_run(first: int, count: int) -> bytes:
    """Give a STAGE payload: an LED run of count dim red LEDs from first."""
    pixels = bytes([8, 0, 0]) * count
    return bytes([hl.ADDRESSING_LED_RUN | hl.PIXEL_RGB << 4]) + struct.pack('<HB', first, count) + pixels


def page(number: int, start: int = 0, token: int = 0, mask: int = 0) -> bytes:
    """Give a GET_PAGE payload."""
    return struct.pack('<BHIH', number, start, token, mask)


# -- the scenarios -------------------------------------------------------------------------------------------------
# Each takes a driver at an unclaimed board and leaves the claim given up. Its docstring is the vector's situation.

def hello(d: Driver) -> None:
    """HELLO: the magic, the version, the reply queue limit and the capabilities."""
    d.req('HELLO', hl.CMD_HELLO)


def claim_echo(d: Driver) -> None:
    """CLAIM's reply echoes the action and token and shows the holder; page 1 follows."""
    d.page1('page 1 before')
    r = claim(d, 'A claims', hl.CLAIM_TAKE, A, NAME_A)
    d.page1('page 1 after')
    claim(d, 'A gives up', hl.CLAIM_GIVE_UP, A, b'', as_holder(gen_of(r)))


def other_token_under_shared_claim(d: Driver) -> None:
    """Under A's shared claim, B's CLAIM and give up are answered OK and change nothing."""
    r = claim(d, 'A claims', hl.CLAIM_TAKE, A, NAME_A)
    claim(d, 'B asks for the claim: OK, A shown', hl.CLAIM_TAKE, B, NAME_B)
    claim(d, 'B gives up: OK, nothing changes', hl.CLAIM_GIVE_UP, B, b'')
    claim(d, 'A gives up', hl.CLAIM_GIVE_UP, A, b'', as_holder(gen_of(r)))


def change_of_holder_resets(d: Driver) -> None:
    """A configures and shows a frame; B takes over: the generation steps, the takeover ends, settings return."""
    r = claim(d, 'A claims', hl.CLAIM_TAKE, A, NAME_A)
    g = as_holder(gen_of(r))
    configure(d, 'A configures overlay, 500 ms, brightness not applied', g, 1, 500, 0)
    d.req('A fills and publishes', hl.CMD_FILL, FILL_RED, g | hl.FLAG_COMMIT_AFTER)
    d.page1('page 1: A holds, frame shown')
    t = claim(d, 'B takes over', hl.CLAIM_TAKE_OVER, B, NAME_B)
    d.page1('page 1: B holds, no frame, settings back to the defaults')
    d.req("A's COMMIT under its old generation: status 4, B shown", hl.CMD_COMMIT, b'', g)
    d.req("A's HELLO under its old generation: status 4, reads included", hl.CMD_HELLO, b'', g)
    d.req('a CLEAR under generation 0 acts', hl.CMD_CLEAR, b'', 0)
    claim(d, 'B gives up', hl.CLAIM_GIVE_UP, B, b'', as_holder(gen_of(t)))


def exclusive_claim(d: Driver) -> None:
    """Under A's exclusive claim: what a request without HOLDER and the generation can and cannot do."""
    r = claim(d, 'A claims exclusively', hl.CLAIM_TAKE, A, NAME_A, cflags=hl.CLAIM_EXCLUSIVE)
    g = as_holder(gen_of(r), exclusive=True)
    d.req('STAGE without HOLDER: status 4', hl.CMD_STAGE, bytes([hl.ADDRESSING_LED_RUN]) +
          struct.pack('<HB', 0, 1) + bytes([1, 2, 3]))
    d.req('HELLO without flags: OK', hl.CMD_HELLO)
    d.page1('GET_PAGE without flags: OK')
    d.req('SUBSCRIBE without flags: OK', hl.CMD_SUBSCRIBE, struct.pack('<H', 0))
    d.req('GET_PAGE with KEEPALIVE: status 4', hl.CMD_GET_PAGE, page(hl.PAGE_STATE), hl.FLAG_KEEPALIVE)
    claim(d, 'B asks for the claim: never refused, A shown', hl.CLAIM_TAKE, B, NAME_B)
    d.req('A with HOLDER and its generation: OK', hl.CMD_CLEAR, b'', g)
    claim(d, 'A gives up', hl.CLAIM_GIVE_UP, A, b'', g)


def keepalive_by_holder(d: Driver) -> None:
    """The holder's renewals with KEEPALIVE keep its frame shown past the timeout."""
    r = claim(d, 'A claims', hl.CLAIM_TAKE, A, NAME_A)
    g = as_holder(gen_of(r))
    configure(d, 'A configures 300 ms', g, 0, 300, 1)
    d.req('A fills and publishes', hl.CMD_FILL, FILL_RED, g | hl.FLAG_COMMIT_AFTER)
    for i in range(4):
        d.wait(0.15)
        claim(d, f'A renews with KEEPALIVE ({i + 1})', hl.CLAIM_TAKE, A, NAME_A, g | hl.FLAG_KEEPALIVE)
    d.page1('page 1 after 0.6 s: frame still shown')
    claim(d, 'A gives up', hl.CLAIM_GIVE_UP, A, b'', g)


def other_claims_flags(d: Driver, exclusive: bool = False) -> None:
    """B's CLAIM with KEEPALIVE refreshes nothing, and with COMMIT_AFTER publishes nothing."""
    r = claim(d, 'A claims' + (' exclusively' if exclusive else ''), hl.CLAIM_TAKE, A, NAME_A,
              cflags=hl.CLAIM_EXCLUSIVE if exclusive else 0)
    g = as_holder(gen_of(r), exclusive)
    configure(d, 'A configures 300 ms', g, 0, 300, 1)
    d.req('A fills, not published', hl.CMD_FILL, FILL_RED, g)
    claim(d, "B's CLAIM with COMMIT_AFTER: OK, A shown", hl.CLAIM_TAKE, B, NAME_B, hl.FLAG_COMMIT_AFTER)
    d.page1('page 1: no frame shown')
    d.req('A publishes', hl.CMD_COMMIT, b'', g)
    for i in range(4):
        claim(d, f'A renews without KEEPALIVE ({i + 1})', hl.CLAIM_TAKE, A, NAME_A, g,
              hl.CLAIM_EXCLUSIVE if exclusive else 0)
        claim(d, f"B's CLAIM with KEEPALIVE ({i + 1})", hl.CLAIM_TAKE, B, NAME_B, hl.FLAG_KEEPALIVE)
        d.wait(0.15)
    d.page1('page 1 after 0.6 s: the frame ended at its timeout, A still holds')
    claim(d, 'A gives up', hl.CLAIM_GIVE_UP, A, b'', g)


def other_claims_flags_exclusive(d: Driver) -> None:
    """Under an exclusive claim: B's CLAIM with KEEPALIVE refreshes nothing, and with COMMIT_AFTER publishes nothing."""
    other_claims_flags(d, exclusive=True)


def lost_claim_reply(d: Driver) -> None:
    """A CLAIM with its reply lost took the claim: the same CLAIM again shows it held, the generation unchanged."""
    claim(d, 'A claims, the reply lost (sent with NO_REPLY)', hl.CLAIM_TAKE, A, NAME_A, hl.FLAG_NO_REPLY)
    d.page1('page 1: A holds')
    r = claim(d, 'A claims again: OK, A shown, the same generation', hl.CLAIM_TAKE, A, NAME_A)
    claim(d, 'A gives up', hl.CLAIM_GIVE_UP, A, b'', as_holder(gen_of(r)))


def takeover_between_claim_and_configure(d: Driver) -> None:
    """B takes over between A's CLAIM and CONFIGURE: A's CONFIGURE is refused and shows B."""
    r = claim(d, 'A claims', hl.CLAIM_TAKE, A, NAME_A)
    t = claim(d, 'B takes over', hl.CLAIM_TAKE_OVER, B, NAME_B)
    configure(d, "A's CONFIGURE under its generation: status 4, B shown", as_holder(gen_of(r)), 1, 1000, 1)
    d.page1('page 1: B holds, the defaults')
    claim(d, 'B gives up', hl.CLAIM_GIVE_UP, B, b'', as_holder(gen_of(t)))


def lapse_taken_again_by_a_renewal(d: Driver) -> None:
    """A's claim lapses; its renewal sent with NO_REPLY takes it again; its next request is refused and shows it."""
    r = claim(d, 'A claims', hl.CLAIM_TAKE, A, NAME_A)
    g = as_holder(gen_of(r))
    configure(d, 'A configures 250 ms', g, 0, 250, 1)
    d.wait(0.4)
    d.page1('page 1 after 0.4 s: the lease lapsed, unclaimed, the generation stepped')
    claim(d, 'A renews with NO_REPLY: takes the claim again', hl.CLAIM_TAKE, A, NAME_A, g | hl.FLAG_NO_REPLY)
    s = d.req('A commits under its old generation: status 4, A shown under the new generation', hl.CMD_COMMIT, b'', g)
    claim(d, 'A gives up', hl.CLAIM_GIVE_UP, A, b'', as_holder(s[3 + 21] if s else 0))


def led_limit(d: Driver) -> None:
    """Staging at the addressable LED limit: a run up to it is staged, a run past it is refused whole."""
    r = claim(d, 'A claims', hl.CLAIM_TAKE, A, NAME_A)
    g = as_holder(gen_of(r))
    s = d.req('page 8, the Summary group: the LED limit at +2..3', hl.CMD_GET_PAGE, page(hl.PAGE_SUMMARY, mask=1))
    limit = struct.unpack_from('<H', s, RECORDS + 2)[0]
    d.req('STAGE a run of 1 on the last LED below the limit: staged', hl.CMD_STAGE, led_run(limit - 1, 1), g)
    d.req('STAGE a run of 2 from the last LED: status 2 at [6], the count', hl.CMD_STAGE, led_run(limit - 1, 2), g)
    d.req('STAGE a run from the limit: status 2 at [4], the first LED', hl.CMD_STAGE, led_run(limit, 1), g)
    d.req('COMMIT: the LED staged is shown', hl.CMD_COMMIT, b'', g)
    d.page1('page 1: frame shown')
    claim(d, 'A gives up', hl.CLAIM_GIVE_UP, A, b'', g)


def stale_token(d: Driver) -> None:
    """A page read under a token other than the current one: status 3 with the page and start, then read under it."""
    token = struct.unpack_from('<I', d.page1('page 1: the state token at [60..63]'), 60)[0]
    d.req('page 10 under another token: status 3, the page, start and current token', hl.CMD_GET_PAGE,
          page(hl.PAGE_CONTROLS, token=token ^ 0x5A5A5A5A))
    d.req('page 10 under the current token: OK', hl.CMD_GET_PAGE, page(hl.PAGE_CONTROLS, token=token))
    d.req('page 10 from record 1 under another token: status 3', hl.CMD_GET_PAGE,
          page(hl.PAGE_CONTROLS, 1, token ^ 0x5A5A5A5A))
    d.req('page 10 from record 1 under the current token: OK', hl.CMD_GET_PAGE, page(hl.PAGE_CONTROLS, 1, token))
    d.req('page 3 without a token: OK', hl.CMD_GET_PAGE, page(hl.PAGE_NAMES))


def state_changed(d: Driver) -> None:
    """After SUBSCRIBE, a claim and its give up each raise one STATE_CHANGED, carrying page 1 and the token."""
    d.req('SUBSCRIBE to STATE_CHANGED: the events enabled', hl.CMD_SUBSCRIBE,
          struct.pack('<H', 1 << hl.EVENT_STATE_CHANGED))
    d.page1('page 1 after subscribing')
    r = claim(d, 'A claims: one STATE_CHANGED, A holding', hl.CLAIM_TAKE, A, NAME_A)
    d.wait(0.05)
    d.page1('page 1: A holds')
    claim(d, 'A gives up: one STATE_CHANGED, unclaimed', hl.CLAIM_GIVE_UP, A, b'', as_holder(gen_of(r)))
    d.wait(0.05)
    d.page1('page 1: unclaimed')


def idle_and_set_animation(d: Driver) -> None:
    """Model only. The idle flag, and SET_ANIMATION: the same animation, another one, and off."""
    d.req('SUBSCRIBE to STATE_CHANGED', hl.CMD_SUBSCRIBE, struct.pack('<H', 1 << hl.EVENT_STATE_CHANGED))
    d.page1('page 1 before')
    d.idle()
    d.page1('page 1: the animations idle (bit 2), after the event')
    d.req('SET_ANIMATION the animation shown: nothing changes, still idle', hl.CMD_SET_ANIMATION, struct.pack('<H', 0))
    d.req('SET_ANIMATION another animation: shown at once, still idle', hl.CMD_SET_ANIMATION, struct.pack('<H', 1))
    d.wait(0.05)
    d.page1('page 1 after the next render tick: awake, after its event')
    d.idle()
    d.req('SET_ANIMATION off: stays idle', hl.CMD_SET_ANIMATION, struct.pack('<H', hl.ANIMATION_OFF))
    d.page1('page 1: off and idle')


def keyboard_mapping(d: Driver) -> None:
    """Page 11 in two reads, and SET_KEYBOARD_MAPPING refused at [4] or [6], or for profile 0, unwritten."""
    first = d.req('page 11 from record 0: sixteen records', hl.CMD_GET_PAGE, page(hl.PAGE_KEYBOARD_MAPPING))
    d.req('page 11 from record 16: the other sixteen', hl.CMD_GET_PAGE, page(hl.PAGE_KEYBOARD_MAPPING, 16))
    # first records and counts past any board's records, which one report bounds, so a replay writes nothing
    d.req('SET_KEYBOARD_MAPPING from record 0xFFFF, past the last: status 2 at [4]', hl.CMD_SET_KEYBOARD_MAPPING,
          struct.pack('<BHB', 1, 0xFFFF, 1))
    d.req('SET_KEYBOARD_MAPPING of no records: status 2 at [6]', hl.CMD_SET_KEYBOARD_MAPPING,
          struct.pack('<BHB', 1, 0, 0))
    d.req('SET_KEYBOARD_MAPPING of 255 records, past the last: status 2 at [6]', hl.CMD_SET_KEYBOARD_MAPPING,
          struct.pack('<BHB', 1, 0, 0xFF))
    d.req('SET_KEYBOARD_MAPPING with profile, first record and count wrong: status 2 at [4], the range checked '
          'before the profile', hl.CMD_SET_KEYBOARD_MAPPING, struct.pack('<BHB', 0, 0xFFFF, 0))
    # record 0's code as the board shows it, so even a board that does not check the profile writes nothing new
    d.req('SET_KEYBOARD_MAPPING for profile 0, never active: status 3, nothing written',
          hl.CMD_SET_KEYBOARD_MAPPING, struct.pack('<BHB', 0, 0, 1) + bytes([first[RECORDS + 2]]))


def keyboard_code_change(d: Driver) -> None:
    """Model only. The code shown writes nothing. A new code changes page 11, its counter and the token."""
    d.req('SUBSCRIBE to STATE_CHANGED', hl.CMD_SUBSCRIBE, struct.pack('<H', 1 << hl.EVENT_STATE_CHANGED))
    d.req('page 8: the change counters', hl.CMD_GET_PAGE, page(hl.PAGE_SUMMARY))
    d.req('SET_KEYBOARD_MAPPING record 4 (B1) to its code shown, 0xE1: OK, nothing changes',
          hl.CMD_SET_KEYBOARD_MAPPING, struct.pack('<BHB', 1, 4, 1) + bytes([0xE1]))
    d.req('SET_KEYBOARD_MAPPING record 4 to 0x04: OK, then one STATE_CHANGED with a new token',
          hl.CMD_SET_KEYBOARD_MAPPING, struct.pack('<BHB', 1, 4, 1) + bytes([0x04]))
    d.wait(0.05)
    d.req('page 11: record 4 has code 0x04', hl.CMD_GET_PAGE, page(hl.PAGE_KEYBOARD_MAPPING))
    d.req("page 8: page 11's change counter stepped", hl.CMD_GET_PAGE, page(hl.PAGE_SUMMARY))
    d.req('SET_KEYBOARD_MAPPING record 4 back to 0xE1: OK, the token returns', hl.CMD_SET_KEYBOARD_MAPPING,
          struct.pack('<BHB', 1, 4, 1) + bytes([0xE1]))
    d.wait(0.05)


def full_reply_queue(d: Driver) -> None:
    """Model only. 16 replies wait unread; the 17th request needing a reply is discarded, neither run nor answered."""
    for i in range(16):
        d.send(f'HELLO {i + 1}, its reply left unread', hl.CMD_HELLO)
    d.req('HELLO 17 with 16 replies waiting: discarded (no reply of its own among them)', hl.CMD_HELLO)


SCENARIOS: list[Callable[[Driver], None]] = [
    hello, claim_echo, other_token_under_shared_claim, change_of_holder_resets, exclusive_claim, keepalive_by_holder,
    other_claims_flags, other_claims_flags_exclusive, lost_claim_reply, takeover_between_claim_and_configure,
    lapse_taken_again_by_a_renewal, led_limit, stale_token, state_changed, idle_and_set_animation, keyboard_mapping,
    keyboard_code_change, full_reply_queue]
NAMES = {other_claims_flags: 'other_claims_flags_shared'}
MODEL_ONLY = {idle_and_set_animation, keyboard_code_change, full_reply_queue}
EVENTS_COMPARED = {state_changed, keyboard_code_change}
IDLE_COMPARED = {idle_and_set_animation}
SETUP = {idle_and_set_animation: SETUP_SECOND_ANIMATION}


def name_of(scenario: Callable[[Driver], None]) -> str:
    """Give a scenario's vector name."""
    return NAMES.get(scenario, scenario.__name__)


# -- the model's vectors -------------------------------------------------------------------------------------------

def generate() -> dict[str, Any]:
    """Run every scenario on a fresh model board and return the vectors document."""
    vectors = []
    for scenario in SCENARIOS:
        d = Model(animations=2 if scenario is idle_and_set_animation else 1)
        gen0, number0 = start_of(d)
        scenario(d)
        vectors.append({'name': name_of(scenario), 'situation': (scenario.__doc__ or '').strip(),
                        'model_only': scenario in MODEL_ONLY, 'events_compared': scenario in EVENTS_COMPARED,
                        'idle_compared': scenario in IDLE_COMPARED, 'setup': SETUP.get(scenario),
                        'generation_at_start': gen0, 'takeover_number_at_start': number0, 'steps': d.steps})
    return {'protocol': '2.0', 'source': 'gp2040ce_hostlighting.testing.ModelBoard',
            'reports': 'hex, 64 bytes each, without the leading report ID 0x00 Windows adds', 'vectors': vectors}


# -- comparing a board with the model -----------------------------------------------------------------------------

def _holder(r: bytes, at: int, gen0: int) -> tuple[Any, ...]:
    """Give the holder at an offset: token, name, exclusive flag, and the generation as steps from the start."""
    token = struct.unpack_from('<I', r, at)[0]
    return (token, r[at + 4:at + 20].rstrip(b'\x00'), r[at + 20] & 1, (r[at + 21] - gen0) % 7)


def _state(r: bytes, gen0: int, number0: int, idle: bool) -> tuple[Any, ...]:
    """Give page 1's record, in a reply or an event: its flags, takeover number, holder and settings."""
    settings = HOLDER + CONTROLLER_SIZE
    flags = r[RECORDS] & (hl.STATE_LIVE | hl.STATE_UNSAVED | (hl.STATE_IDLE if idle else 0))
    return ((flags, (r[RECORDS + 7] - number0) % 256) + _holder(r, HOLDER, gen0) +
            (r[settings:settings + SETTINGS_SIZE].hex(),))


def view(report: str, gen0: int, number0: int, idle: bool = False) -> tuple[Any, ...]:
    """Give what a board's own state cannot change in a report, with page 1's idle flag when asked."""
    r = bytes.fromhex(report)
    command, status = r[0] & 0x7F, r[2]
    if r[0] == hl.EVENT_MARKER:
        return ('event', r[1]) + _state(r, gen0, number0, idle)
    if status == hl.STATUS_INVALID:
        return (command, status, r[3])
    if status == hl.STATUS_CLAIMED:
        return (command, status) + _holder(r, 3, gen0)
    if command == hl.CMD_CLAIM:
        return (command, status, r[3:8].hex()) + _holder(r, 8, gen0)
    if command == hl.CMD_GET_PAGE:
        echo = (command, status, r[3], r[4:6].hex())
        return echo + _state(r, gen0, number0, idle) if r[3] == hl.PAGE_STATE and status == hl.STATUS_OK else echo
    if command == hl.CMD_HELLO:
        return (command, status, r[3:9].hex(), r[17], r[18:45].hex())
    if command == hl.CMD_STAGE:
        return (command, status, r[3], r[4], r[5:9].hex())
    return (command, status)


def start_of(d: Driver) -> tuple[int, int]:
    """Read page 1 unrecorded: the generation and takeover number the scenario starts from."""
    d.recording = False
    r = d.page1()
    d.recording = True
    return r[HOLDER + 21], r[RECORDS + 7]


def reset(d: Driver) -> None:
    """Leave the board unclaimed, with no frame and the defaults, unrecorded, and drop the events that follow."""
    d.recording = False
    for token in (A, B):
        r = d.page1()
        if struct.unpack_from('<I', r, HOLDER)[0] == token:
            claim(d, 'reset', hl.CLAIM_GIVE_UP, token, b'', as_holder(r[HOLDER + 21], bool(r[HOLDER + 20] & 1)))
    d.req('reset', hl.CMD_RELEASE)
    d.req('reset', hl.CMD_CLEAR, settle=0.1)
    d.recording = True


def compare(vector: dict[str, Any], steps: list[dict[str, Any]], gen0: int, number0: int) -> list[str]:
    """List how a board's steps differ from the vector's, empty when they do not."""
    idle = vector['idle_compared']

    def replies(reports: list[str], g: int, n: int) -> list[tuple[Any, ...]]:
        return [view(r, g, n, idle) for r in reports if bytes.fromhex(r)[0] != hl.EVENT_MARKER]

    def events(all_steps: list[dict[str, Any]], g: int, n: int) -> list[tuple[Any, ...]]:
        return [view(r, g, n, idle) for step in all_steps for r in step['reports']
                if bytes.fromhex(r)[0] == hl.EVENT_MARKER]

    g0, n0 = vector['generation_at_start'], vector['takeover_number_at_start']
    bad = [f"  {want['label']}: model {replies(want['reports'], g0, n0)} board {replies(got['reports'], gen0, number0)}"
           for want, got in zip(vector['steps'], steps)
           if replies(want['reports'], g0, n0) != replies(got['reports'], gen0, number0)]
    if len(vector['steps']) != len(steps):
        bad.append(f"  steps: model {len(vector['steps'])} board {len(steps)}")
    if vector['events_compared'] and events(vector['steps'], g0, n0) != events(steps, gen0, number0):
        bad.append(f"  events: model {events(vector['steps'], g0, n0)} board {events(steps, gen0, number0)}")
    return bad


def run_vector(board: Driver, vector: dict[str, Any], scenario: Callable[[Driver], None]) -> list[str]:
    """Run one scenario between resets, and list how the board differs from the vector or what ended it."""
    reset(board)
    board.steps = []
    try:
        gen0, number0 = start_of(board)
        scenario(board)
    except Exception as error:  # noqa: B902 any fault ends this scenario only
        return [f"  ended by {type(error).__name__}: {error}"]
    finally:
        reset(board)
    return compare(vector, board.steps, gen0, number0)


def replay_on(board: Driver, vectors: list[dict[str, Any]], scenarios: dict[str, Callable[[Driver], None]]) -> int:
    """Run each vector a board can show, print the comparison, and return how many differ."""
    failed = 0
    for vector in vectors:
        if vector['model_only']:
            print(f"{vector['name']}: model only")
            continue
        bad = run_vector(board, vector, scenarios[vector['name']])
        failed += bool(bad)
        print(f"{vector['name']}: {'FAIL' if bad else 'same'}")
        for line in bad:
            print(line)
    print(f"RESULT: {'PASS' if not failed else str(failed) + ' FAILED'}")
    return failed


def replay(board_id: str) -> int:
    """Replay the vectors on the board with this ID and return how many differ."""
    with open(VECTORS, encoding='utf-8') as f:
        vectors = json.load(f)['vectors']
    board = Board(board_id)
    try:
        return replay_on(board, vectors, {name_of(scenario): scenario for scenario in SCENARIOS})
    finally:
        board.device.close()


def main(argv: Optional[list[str]] = None) -> int:
    """Write the vectors from the model, or replay them on a board with --board ID."""
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ['--board'] and len(args) == 2:
        return 1 if replay(args[1]) else 0
    if args:
        raise SystemExit(__doc__)
    document = generate()
    with open(VECTORS, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(document, f, indent=1)
        f.write('\n')
    vectors = document['vectors']
    print(len(vectors), 'vectors,', sum(len(v['steps']) for v in vectors), 'steps written to', VECTORS)
    return 0


if __name__ == '__main__':
    sys.exit(main())
