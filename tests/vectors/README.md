<!--
gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
Filename: README.md
Folder: tests/vectors
https://github.com/djGLiTCH/gp2040ce-hostlighting
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

# HLP 2.0 test vectors

Exact request and reply reports for HELLO, the claim and its refusals,
`KEEPALIVE`, lost replies, staging at the LED limit, a stale state token,
`STATE_CHANGED`, the idle flag, the keyboard mapping and
`SET_KEYBOARD_MAPPING` with keyboard mapping changes allowed or not, and the
reply queue, for hosts that test against their own board model.
`vectors.json` is generated from the library's `ModelBoard` by
`scripts/test_vectors.py`. A test keeps the two in step.

    python scripts/test_vectors.py               write the vectors
    python scripts/test_vectors.py --board ID    replay them on a board

## Format

| Key | Holds |
|---|---|
| `vectors[].name` | The scenario |
| `vectors[].situation` | One line: what it shows |
| `vectors[].model_only` | True where a board cannot be made to show it on demand |
| `vectors[].events_compared` | True where a replay compares the events, not only the replies |
| `vectors[].idle_compared` | True where a replay compares page 1's idle flag (State `+0` bit 2) |
| `vectors[].setup` | What the board needs, as a code below, or null |
| `vectors[].generation_at_start`, `takeover_number_at_start` | Page 1's claim generation and takeover number before the first step |
| `vectors[].steps[]` | One request each, below |
| `steps[].label` | What the step shows |
| `steps[].wait_before` | Seconds to wait before sending the request |
| `steps[].board_before` | What the board does by itself before the request, as a code below, or null. Only the model can be made to |
| `steps[].request` | The request, 64 bytes, hex |
| `steps[].read` | False where the host sends the request and reads nothing; its reports come in a later step |
| `steps[].reports` | Every report the board sent after the request, replies and events, in order |

The codes are fixed; a replay acts on them.

| Code | In | Means |
|---|---|---|
| `second_animation` | `setup` | The board has a second animation (page 13), so `SET_ANIMATION` can change it |
| `keyboard_mapping_changes` | `setup` | The board's settings allow hosts to change the keyboard mapping, so `HELLO` lists `SET_KEYBOARD_MAPPING` |
| `no_keyboard_mapping_changes` | `setup` | The board's settings do not allow it, their default, so `HELLO` does not list `SET_KEYBOARD_MAPPING` |
| `idle_timeout` | `board_before` | The animations' idle timeout fires, turning them off and setting page 1's idle flag |

A replay runs a vector with `keyboard_mapping_changes` only on a board that
lists `SET_KEYBOARD_MAPPING` in `HELLO`, and one with
`no_keyboard_mapping_changes` only on a board that does not.

Reports are hex without the report ID `0x00` that Windows adds. Each scenario
starts with the board unclaimed, no frame shown and the `CONFIGURE` defaults,
and ends the same. Token A is `0x48A1A1A1` (`vec-a`), token B `0x48B2B2B2`
(`vec-b`).

## What varies between boards

A board's own state changes some bytes. Compare these as steps from the
scenario's start, or not at all:

- the claim generation (`CLAIM` reply `[29]`, status 4 reply `[24]`, page 1
  `+31`) and the takeover number (page 1 `+7`): as steps from the start
- the board ID (`HELLO` `[9..16]`), the subscription (`HELLO` `[45..46]`), the
  state token (page replies `[60..63]`), the records of every page but page 1,
  and page 1's input mode, profile, player, animation, brightness and D-pad
  mode fields
- the LED limit (page 8 `+2..3`): `led_limit` stages at the limit page 8
  gives, so its `STAGE` requests carry the model's limit of 100 and a replay
  sends the board's
- the state token in a request: `stale_token`'s `GET_PAGE` requests carry the
  model's token and a token other than it, so a replay sends its board's
  token, read from page 1 `[60..63]`, and a token other than that
- the code in `keyboard_mapping`'s profile 0 step and in both of
  `keyboard_mapping_not_allowed`'s `SET_KEYBOARD_MAPPING` steps: a replay
  sends its board's page 11 record 0 code, read in the scenario's page 11
  step, so the steps write nothing new even on a board that does not check
  the profile, or that runs the command
- the idle flag (page 1 State `+0` bit 2): the board's own idle timeout can
  set it at any time, so it is compared only where `idle_compared` is true
- `HELLO`'s command bit 0x34 (`SET_KEYBOARD_MAPPING`, `[25]` bit 4): it
  follows the board's settings, so it is compared only in the vectors with
  `keyboard_mapping_changes` or `no_keyboard_mapping_changes`
- events: when one arrives between replies depends on timing

A replay compares each reply's command and status, a status 2 offset, the
holder a status 4 or `CLAIM` reply shows, `CLAIM`'s echo, `HELLO`'s magic,
version, reply queue limit and capabilities, `STAGE`'s outcome, a page reply's
page and start, and page 1's frame-shown and unsaved bits, takeover number,
holder and settings. Where `idle_compared` is true it compares page 1's idle
flag too. Where `events_compared` is true it compares the events in order, as
page 1 is compared.

## Vectors

| Name | Shows |
|---|---|
| `hello` | `HELLO`: the magic, the version, the reply queue limit and the capabilities |
| `claim_echo` | The `CLAIM` reply's echo and holder; page 1 before and after |
| `other_token_under_shared_claim` | Another token's `CLAIM` and give up under a shared claim: OK, nothing changes |
| `change_of_holder_resets` | A take over: the generation steps, the takeover ends, the settings return; the old generation refused, reads included |
| `exclusive_claim` | Each opening and refusal of an exclusive claim |
| `keepalive_by_holder` | The holder's renewals with `KEEPALIVE` keep its frame shown |
| `other_claims_flags_shared`, `other_claims_flags_exclusive` | Another token's `CLAIM` with `COMMIT_AFTER` publishes nothing and with `KEEPALIVE` refreshes nothing |
| `lost_claim_reply` | A `CLAIM` with its reply lost took the claim; the same `CLAIM` again shows it, the generation unchanged |
| `takeover_between_claim_and_configure` | Another application takes over before `CONFIGURE`: refused, the holder shown |
| `lapse_taken_again_by_a_renewal` | A lapse; a renewal with `NO_REPLY` takes the claim again; the next request is refused and shows the new generation |
| `led_limit` | An LED run on the last LED below the limit is staged; a run reaching past it is refused with the offset of its count, and a run from the limit with the offset of its first LED |
| `stale_token` | A page read under another token: status 3 with the page and start; the same read under the current token; page 3 without a token |
| `state_changed` | After `SUBSCRIBE`, a claim and its give up each raise one `STATE_CHANGED`, laid out as page 1 |
| `idle_and_set_animation` | Model only. The idle flag and its events; `SET_ANIMATION` with the animation shown, another (shown at once, awake at the next render tick), and off |
| `keyboard_mapping` | Page 11 in two reads. `SET_KEYBOARD_MAPPING` from past the last record is refused at [4], of no records or of records past the last at [6], and with the profile, first record and count wrong at [4], the range checked first. For profile 0, never active, it is refused with status 3 |
| `keyboard_mapping_not_allowed` | Settings that do not allow keyboard mapping changes. `HELLO` without `SET_KEYBOARD_MAPPING`, and page 11 read. `SET_KEYBOARD_MAPPING` is status 1, and still status 1, not 4, without `HOLDER` under an exclusive claim |
| `keyboard_code_change` | Model only. `SET_KEYBOARD_MAPPING` of the code shown changes nothing. A new code steps page 11's counter and the token, with one `STATE_CHANGED`, and the old code again puts the token back |
| `full_reply_queue` | Model only. 16 `HELLO` requests are sent and their replies left unread; the 17th, which needs a reply, is discarded |

The settings write's pause is not modelled, and is not among the vectors.
