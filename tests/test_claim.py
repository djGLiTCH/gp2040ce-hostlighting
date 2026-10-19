"""Test LightsClaim: taking, renewing, following and giving up the claim.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import logging
import struct
from types import SimpleNamespace

import pytest

from gp2040ce_hostlighting import claim as claim_module
from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.claim import ClaimChange, ClaimChangeKind, ClaimState, LightsClaim
from gp2040ce_hostlighting.records import Controller, Settings
from gp2040ce_hostlighting.testing import other_event, page_reply, state_event, state_record

TOKEN = 0x51


class Clock:
    """A monotonic clock the test moves."""

    def __init__(self):
        """Start at 0 s."""
        self.now = 0.0

    def __call__(self) -> float:
        """Read the clock."""
        return self.now


@pytest.fixture
def clock():
    """Make a clock at 0 s."""
    return Clock()


def lights_claim(device, clock, settings=Settings(), **options) -> LightsClaim:
    """Make a claim with a known token on the test's clock."""
    return LightsClaim(device, 'app', settings, token=TOKEN, clock=clock, **options)


def claims(board) -> list:
    """Each CLAIM sent: (action, flags without the generation, claim flags)."""
    return [(request[3], request[2] & 0x0F, request[24]) for request in board.sent(hl.CMD_CLAIM)]


def holder(token, generation, name='', exclusive=False) -> Controller:
    """Build the holder a reply shows."""
    return Controller(holder_token=token, application_name=name, exclusive=exclusive, claim_generation=generation)


def test_take_claims_then_sends_the_settings(hlp_fake_device, hlp_fake_board, clock):
    """Test that take() claims, sends CONFIGURE and paces the renewals at a third of the timeout applied."""
    claim = lights_claim(hlp_fake_device, clock, Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=900))
    assert claim.state is ClaimState.ENDED and claim.seconds_to_renewal is None
    assert claim.take() is claim and claim.state is ClaimState.HELD
    assert claims(hlp_fake_board) == [(hl.CLAIM_TAKE, 0, 0)]
    assert hlp_fake_board.requests[-1][:1] == bytes([hl.CMD_CONFIGURE])
    assert hlp_fake_board.requests[-1][3:7] == struct.pack('<BHB', hl.TAKEOVER_OVERLAY, 900, 1)
    assert claim.applied == Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=900)
    assert claim.seconds_to_renewal == pytest.approx(0.3)
    assert claim.holder == holder(TOKEN, 2, 'app') and hlp_fake_device.generation == 2


def test_take_makes_a_random_token_and_checks_its_arguments(hlp_fake_device):
    """Test the random non-zero token, and the refusal of a long name or a token out of range."""
    assert 0 < LightsClaim(hlp_fake_device, 'app').token <= 0xFFFFFFFF
    with pytest.raises(ValueError, match='18 bytes'):
        LightsClaim(hlp_fake_device, 'café-' + 'x' * 12)        # 17 characters, 18 bytes
    for token in (0, 1 << 32):
        with pytest.raises(ValueError, match='1 to 0xFFFFFFFF'):
            LightsClaim(hlp_fake_device, 'app', token=token)


def test_take_refuses_another_holder(hlp_fake_device, hlp_fake_board, clock):
    """Test that another holder raises HostLightingClaimedError, naming it, and nothing is configured."""
    hlp_fake_board.take_over(0xA1, b'MESH')
    claim = lights_claim(hlp_fake_device, clock)
    with pytest.raises(hl.HostLightingClaimedError, match=r"'MESH' holds the lights \(claim 0x000000A1, "
                                                          r"generation 2\)") as refused:
        claim.take()
    assert refused.value.holder == holder(0xA1, 2, 'MESH') and claim.state is ClaimState.ENDED
    assert not hlp_fake_board.sent(hl.CMD_CONFIGURE) and hlp_fake_board.holder == 0xA1


def test_take_with_force_takes_the_claim_over(hlp_fake_device, hlp_fake_board, clock):
    """Test that force sends CLAIM take over, and exclusive claims exclusively, renewals included."""
    hlp_fake_board.take_over(0xA1, b'MESH')
    claim = lights_claim(hlp_fake_device, clock).take(exclusive=True, force=True)
    clock.now = 1.0
    claim.maintain()
    assert claims(hlp_fake_board) == [(hl.CLAIM_TAKE_OVER, 0, hl.CLAIM_EXCLUSIVE),
                                      (hl.CLAIM_TAKE, hl.FLAG_KEEPALIVE | hl.FLAG_HOLDER, hl.CLAIM_EXCLUSIVE)]
    assert hlp_fake_board.holder == TOKEN and hlp_fake_board.holder_flags == hl.CLAIM_EXCLUSIVE


def test_take_waits_for_another_holder(hlp_fake_device, hlp_fake_board, clock, caplog):
    """Test that wait leaves the claim waiting, asks at each renewal, and takes it once the board is unclaimed."""
    hlp_fake_board.take_over(0xA1, b'MESH')
    with caplog.at_level(logging.INFO, logger='gp2040ce_hostlighting.claim'):
        claim = lights_claim(hlp_fake_device, clock).take(wait=True)
    assert claim.state is ClaimState.WAITING and "'MESH' holds the lights. Waiting" in caplog.text
    assert claim.maintain() == ClaimChange(kind=ClaimChangeKind.UNCHANGED) and len(claims(hlp_fake_board)) == 1
    clock.now = 0.7
    assert claim.maintain().holder.holder_token == 0xA1 and claim.state is ClaimState.WAITING
    hlp_fake_board.let_go()
    clock.now = 1.4
    change = claim.maintain()
    assert change.kind is ClaimChangeKind.TAKEN and change.restage and claim.state is ClaimState.HELD
    assert [action for action, _, _ in claims(hlp_fake_board)] == [hl.CLAIM_TAKE] * 3
    assert claims(hlp_fake_board)[2][1] == 0                 # nothing shown yet: no KEEPALIVE
    assert len(hlp_fake_board.sent(hl.CMD_CONFIGURE)) == 1


def test_a_failed_set_up_gives_the_claim_up(hlp_fake_device, hlp_fake_board, clock):
    """Test that a CONFIGURE refused after the claim was taken gives the claim up before the error is raised."""
    answer = hlp_fake_board.answer

    def refusing(command, sequence, request):
        if command == hl.CMD_CONFIGURE:
            return bytes([command | hl.REPLY_FLAG, sequence, hl.STATUS_INVALID, 3]) + bytes(60)
        return answer(command, sequence, request)
    hlp_fake_board.answer = refusing
    claim = lights_claim(hlp_fake_device, clock)
    with pytest.raises(hl.HostLightingRejectedError):
        claim.take()
    assert claims(hlp_fake_board)[-1][0] == hl.CLAIM_GIVE_UP and hlp_fake_board.holder == 0
    assert claim.state is ClaimState.ENDED


def test_maintain_renews_only_when_due(hlp_fake_device, hlp_fake_board, clock):
    """Test that maintain() sends nothing before a renewal is due, then renews with KEEPALIVE unless it is off."""
    claim = lights_claim(hlp_fake_device, clock).take()
    clock.now = 0.6
    assert claim.maintain().kind is ClaimChangeKind.UNCHANGED and len(claims(hlp_fake_board)) == 1
    clock.now = 0.7
    change = claim.maintain()
    assert change == ClaimChange(kind=ClaimChangeKind.UNCHANGED, holder=holder(TOKEN, 2, 'app'))
    assert claims(hlp_fake_board)[-1] == (hl.CLAIM_TAKE, hl.FLAG_KEEPALIVE, 0)
    assert claim.seconds_to_renewal == pytest.approx(2 / 3)
    claim.keepalive = False
    assert claim.renew().kind is ClaimChangeKind.UNCHANGED and claims(hlp_fake_board)[-1][1] == 0


def test_a_renewal_that_takes_a_lapsed_claim_again_configures(hlp_fake_device, hlp_fake_board, clock, caplog):
    """Test RETAKEN: a new generation sends the settings again and asks for the frame to be staged again."""
    claim = lights_claim(hlp_fake_device, clock, Settings(brightness_applied=False)).take()
    hlp_fake_board.let_go()                                  # the lease lapsed: generation 3
    with caplog.at_level(logging.INFO, logger='gp2040ce_hostlighting.claim'):
        change = claim.renew()
    assert change == ClaimChange(kind=ClaimChangeKind.RETAKEN, holder=holder(TOKEN, 4, 'app'), restage=True)
    assert 'claim taken again under generation 4' in caplog.text
    assert [request[3:7] for request in hlp_fake_board.sent(hl.CMD_CONFIGURE)] == [bytes([0, 0xD0, 0x07, 0])] * 2
    assert hlp_fake_device.generation == 4 and claim.renew().kind is ClaimChangeKind.UNCHANGED


def test_a_claim_taken_over_is_lost(hlp_fake_device, hlp_fake_board, clock, caplog):
    """Test LOST without wait_after_takeover: renewals stop, keep() returns False, and the give up sends nothing."""
    claim = lights_claim(hlp_fake_device, clock, wait_after_takeover=False).take()
    hlp_fake_board.take_over(0xB2, b'Other')
    with caplog.at_level(logging.INFO, logger='gp2040ce_hostlighting.claim'):
        change = claim.renew()
    assert change == ClaimChange(kind=ClaimChangeKind.LOST, holder=holder(0xB2, 3, 'Other'))
    assert claim.state is ClaimState.LOST and "'Other' took the lights over\n" in caplog.text
    clock.now = 10.0
    sent = len(hlp_fake_board.requests)
    assert claim.maintain().kind is ClaimChangeKind.UNCHANGED and claim.renew().kind is ClaimChangeKind.UNCHANGED
    assert not claim.keep(1.0) and not claim.give_up() and len(hlp_fake_board.requests) == sent
    assert claim.state is ClaimState.ENDED and claim.seconds_to_renewal is None


def test_a_claim_taken_over_is_waited_for_by_default(hlp_fake_device, hlp_fake_board, clock, caplog):
    """Test that a claim taken over after it was held waits, and is taken back when the board is unclaimed."""
    claim = lights_claim(hlp_fake_device, clock).take()
    hlp_fake_board.take_over(0xB2, b'Other')
    with caplog.at_level(logging.INFO, logger='gp2040ce_hostlighting.claim'):
        assert claim.renew().kind is ClaimChangeKind.LOST and claim.state is ClaimState.WAITING
    assert "'Other' took the lights over. Waiting for it to give them up" in caplog.text
    hlp_fake_board.let_go()
    assert claim.renew().kind is ClaimChangeKind.TAKEN and hlp_fake_board.holder == TOKEN


def test_observe_renews_at_once_after_another_holder(hlp_fake_device, hlp_fake_board, clock):
    """Test that an event showing another holder, even one followed by this claim's, sends the settings again."""
    claim = lights_claim(hlp_fake_device, clock, wait_after_takeover=False).take()
    ours = state_event(token=TOKEN, name=b'app', generation=2)
    assert claim.observe([ours]).kind is ClaimChangeKind.UNCHANGED and len(claims(hlp_fake_board)) == 1
    # another application held the claim between: the settings and the frame are gone, the generation the same
    change = claim.observe([state_event(token=0xB2, generation=3), ours, other_event()])
    assert change.kind is ClaimChangeKind.RETAKEN and change.restage
    assert len(claims(hlp_fake_board)) == 2 and len(hlp_fake_board.sent(hl.CMD_CONFIGURE)) == 2
    hlp_fake_board.take_over(0xB2, b'Other')
    record = hl.decode_state_changed(state_event(token=0xB2, generation=3))
    assert claim.observe([record]).kind is ClaimChangeKind.LOST and claim.state is ClaimState.LOST
    assert claim.observe([ours]).kind is ClaimChangeKind.UNCHANGED


def test_observe_takes_the_claim_when_the_board_is_unclaimed(hlp_fake_device, hlp_fake_board, clock):
    """Test that a waiting claim asks at once for a board an event or a page 1 record shows unclaimed."""
    hlp_fake_board.take_over(0xA1, b'MESH')
    claim = lights_claim(hlp_fake_device, clock).take(wait=True)
    assert claim.observe([state_event(token=0xA1)]).kind is ClaimChangeKind.UNCHANGED
    hlp_fake_board.let_go()
    page_1 = hl.decode_state_changed(state_event(token=0, generation=3)).record
    assert claim.observe([page_1]).kind is ClaimChangeKind.TAKEN and claim.state is ClaimState.HELD


def test_observe_passes_over_what_shows_no_holder(hlp_fake_device, hlp_fake_board, clock):
    """Test that a cut-short event, a page 1 record without the Controller group and other events change nothing."""
    claim = lights_claim(hlp_fake_device, clock).take()
    cut_short = bytearray(state_event(token=0xB2))
    cut_short[9] = 1                                        # a stride narrower than the groups
    state_alone = hl.decode_records(page_reply(hl.PAGE_STATE, [state_record(token=0xB2)], mask=0b001))[0]
    assert claim.observe([bytes(cut_short), state_alone, other_event()]).kind is ClaimChangeKind.UNCHANGED
    assert len(claims(hlp_fake_board)) == 1


def test_refused_applies_the_status_4_table(hlp_fake_device, hlp_fake_board, clock):
    """Test each row: this claim's token under a new generation, token 0, another holder, and the rest."""
    claim = lights_claim(hlp_fake_device, clock, wait_after_takeover=False).take()

    def refusal(shown, status=hl.STATUS_CLAIMED):
        return hl.HostLightingRejectedError(hl.CMD_STAGE, status, 0, shown)
    assert claim.refused(refusal(None, hl.STATUS_INVALID)).kind is ClaimChangeKind.UNCHANGED
    assert claim.refused(refusal(holder(TOKEN, 2))).kind is ClaimChangeKind.UNCHANGED
    assert len(hlp_fake_board.requests) == 2                 # the claim and CONFIGURE alone
    # a renewal sent without a reply took the lapsed claim again
    change = claim.refused(refusal(holder(TOKEN, 4)))
    assert change.kind is ClaimChangeKind.RETAKEN and change.restage
    assert hlp_fake_board.requests[-1][0] == hl.CMD_CONFIGURE and len(claims(hlp_fake_board)) == 1
    hlp_fake_board.let_go()
    change = claim.refused(refusal(holder(0, 5)))
    assert change.kind is ClaimChangeKind.RETAKEN and hlp_fake_board.holder == TOKEN
    assert claim.refused(refusal(holder(0xB2, 7, 'Other'))).kind is ClaimChangeKind.LOST
    assert claim.state is ClaimState.LOST


def test_configure_renews_first_and_paces_the_renewals(hlp_fake_device, hlp_fake_board, clock):
    """Test configure() while held, while not held, and a change its renewal finds returned by the next call."""
    claim = lights_claim(hlp_fake_device, clock, wait_after_takeover=False)
    assert claim.configure(Settings(keepalive_ms=600)) is None and not hlp_fake_board.requests
    claim.take()
    assert claim.seconds_to_renewal == pytest.approx(0.2)
    applied = claim.configure(Settings(keepalive_ms=300))
    assert applied == Settings(keepalive_ms=300) and claim.seconds_to_renewal == pytest.approx(0.1)
    assert [request[0] for request in hlp_fake_board.requests[2:]] == [hl.CMD_CLAIM, hl.CMD_CONFIGURE]
    hlp_fake_board.let_go()
    assert claim.configure(Settings(keepalive_ms=900)) == Settings(keepalive_ms=900)
    assert [request[0] for request in hlp_fake_board.requests[4:]] == [hl.CMD_CLAIM, hl.CMD_CONFIGURE]
    assert claim.maintain().kind is ClaimChangeKind.RETAKEN and claim.maintain().kind is ClaimChangeKind.UNCHANGED
    hlp_fake_board.take_over(0xB2, b'Other')
    assert claim.configure(Settings()) is None and claim.state is ClaimState.LOST
    assert claim.renew().kind is ClaimChangeKind.LOST


def test_give_up(hlp_fake_device, hlp_fake_board, clock, caplog):
    """Test the give up, the with block's, and one that finds another holder or a board that has gone."""
    with caplog.at_level(logging.INFO, logger='gp2040ce_hostlighting.claim'):
        with lights_claim(hlp_fake_device, clock).take() as claim:
            assert hlp_fake_board.holder == TOKEN
    assert claim.state is ClaimState.ENDED and hlp_fake_board.holder == 0
    assert 'claim given up, on-board animations restored' in caplog.text
    claim.take()
    hlp_fake_board.take_over(0xC3, b'MESH')
    with caplog.at_level(logging.INFO, logger='gp2040ce_hostlighting.claim'):
        assert not claim.give_up()
    assert "'MESH' took the lights over" in caplog.text and hlp_fake_board.holder == 0xC3
    hlp_fake_board.let_go()
    claim.take()
    hlp_fake_board.disconnect_on = {(hl.CMD_CLAIM, hl.CLAIM_GIVE_UP)}
    assert not claim.give_up()
    assert 'could not give the claim up, the board is no longer connected' in caplog.text
    assert 'The keepalive timeout restores the animations' in caplog.text


def test_let_lapse_stops_holding_without_giving_up(hlp_fake_device, hlp_fake_board, clock):
    """Test that let_lapse() sends nothing and leaves the device sending as an application holding no claim."""
    claim = lights_claim(hlp_fake_device, clock).take(exclusive=True)
    sent = len(hlp_fake_board.requests)
    claim.let_lapse()
    assert claim.state is ClaimState.ENDED and len(hlp_fake_board.requests) == sent
    assert (hlp_fake_device.generation, hlp_fake_device.holding_exclusive) == (0, False)
    assert hlp_fake_board.holder == TOKEN
    hlp_fake_device.generation = 5                          # another claim's, which a claim not held leaves alone
    claim.let_lapse()
    assert hlp_fake_device.generation == 5


def test_keep_sleeps_between_renewals(hlp_fake_device, hlp_fake_board, clock, monkeypatch):
    """Test that keep() renews at each third of the timeout, sleeping between, and stops once the claim is lost."""
    slept: list = []

    def sleep(seconds):
        slept.append(round(seconds, 3))
        clock.now += seconds
    monkeypatch.setattr(claim_module, 'time', SimpleNamespace(sleep=sleep, monotonic=clock))
    claim = lights_claim(hlp_fake_device, clock, Settings(keepalive_ms=900), wait_after_takeover=False).take()
    assert claim.keep(1.0) and slept == [0.3, 0.3, 0.3, 0.1]
    assert len(claims(hlp_fake_board)) == 4
    hlp_fake_board.take_over(0xB2, b'Other')
    assert not claim.keep(5.0) and claim.state is ClaimState.LOST
