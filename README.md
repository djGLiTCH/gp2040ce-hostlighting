# gp2040ce-hostlighting

A Python library and tools for the GP2040-CE Host Lighting add-on, which lets host software drive a board's RGB
LEDs live over a vendor HID interface.

## Installation

```
pip install gp2040ce-hostlighting
```

From a clone, `pip install -e .[dev]` installs the commands and the development tools.

## Standalone executables

`packaging/hlp-tools.spec` builds the `hlp-*` commands as Windows executables that run without Python. From a
clone with the development tools installed, run `pip install pyinstaller`, then `pyinstaller
packaging/hlp-tools.spec` in the repository's root. `dist/hlp-tools` then holds one executable per command and
the `_internal` folder they share, which also holds the licences of this package, Python and hidapi.

## Layout

| Path | Holds |
|---|---|
| `gp2040ce_hostlighting/protocol.py` | The protocol: constants, framing, decoders, the device, the claim and discovery |
| `gp2040ce_hostlighting/conformance.py` | The conformance checks and the round trip and frame-rate measurements |
| `gp2040ce_hostlighting/testing.py` | Fake boards and report builders, to test host software without a board |
| `gp2040ce_hostlighting/tools/` | The `hlp-*` commands: `commands.py`, and `conformance.py` for `hlp-conformance` and `hlp-bench` |
| `docs/host-lighting.md` | The Host Lighting specification |
| `tests/` | The tests |

## Specification

`docs/host-lighting.md` is the Host Lighting Protocol 2.0 specification, the document the Host Lighting add-on
adds to GP2040-CE as `docs/host-lighting.md`.

## The library

`gp2040ce_hostlighting.protocol` speaks Host Lighting Protocol 2.0 to a board. It needs only the `hidapi`
package. It never prints, exits or sets up logging. It logs to its own module logger, and each failure raises a
subclass of `HostLightingError`.

A host drives the lights in these steps:

1. Open a board with `open_device()`. With several boards connected, pass a hex prefix of the board ID.
   `find_board()` opens one board by its whole ID and returns `None` when it is not connected.
2. Read what the board reports with `hello()`, `read_identity()`, `read_record()` and `read_table()`. The two
   reads give a `PageRead` of the records and the state token. Each record carries its `ordinal`, its position
   in the page, and one attribute per field group, named after the specification's, such as `light.owner.pin`.
   Reading a group the reply did not return raises `HostLightingGroupNotReadError`, and `record.has(group)`
   says whether it was returned.
3. Take the claim with `LightsClaim(device, name, settings).take()`, where the name is up to 16 bytes.
   `take()` claims, then sends the CONFIGURE settings. While another application holds the claim it raises
   `HostLightingClaimedError`, unless it is to take the claim over (`force=True`) or wait for it (`wait=True`).
4. Stage pixels with `stage_run()` or `stage_entries()`, and publish them with `commit=True`.
5. Call the claim's `maintain()` often. It renews the claim when a renewal is due and returns a `ClaimChange`.
   When the change's `restage` is true, the claim started again with nothing staged, so stage the whole frame
   again. `keep(seconds)` maintains the claim for a while, sleeping between renewals.
6. Give the claim up with `give_up()`, which a `with` block does, then close the device.

The example lights every LED red for 3 s:

```python
from gp2040ce_hostlighting.claim import LightsClaim
from gp2040ce_hostlighting.protocol import (
    ADDRESSING_LED_RUN, PAGE_SUMMARY, open_device)

with open_device() as device:
    leds = device.read_record(PAGE_SUMMARY).records[0].summary.led_extent
    with LightsClaim(device, 'my-host').take() as claim:
        red = [(255, 0, 0)] * leds
        device.stage_run(ADDRESSING_LED_RUN, 0, red, commit=True)
        claim.keep(3)
```

`LightsClaim` logs each change at INFO. Once another application takes the lights over, `maintain()` returns
a change of kind `LOST`, `keep()` returns `False`, and the lights belong to the new holder. `observe()` applies
the holders that `STATE_CHANGED` events show, and `refused()` applies a status 4 refusal to the claim.

A streaming host builds each frame as a `Frame` of entries and runs, staged in the order they were added, and
sends it with `publish_frame()`. Every report but the last goes with `NO_REPLY`, and the last with
`COMMIT_AFTER`, which alone is answered and is sent once more when its answer does not come. The `FrameResult`
says whether the frame was acknowledged.

A host that sends with `send()` and waits with `wait_reply()` reads the reports already received with
`drain()` before each send, takes the events received with `take_events()`, and builds the error for a refused
reply with `HostLightingDevice.rejected()`. The error's `holder` is the holder as `claim()` returns it.

### Testing a host

`gp2040ce_hostlighting.testing` fakes a board, so a host's tests run without one. `fake_device(FakeBoard())`
returns a `HostLightingDevice` connected to a board that answers as the firmware does. `ModelBoard` also follows
the protocol's timing, claim and staging rules, on a clock the test controls with `sleep()` and `time()`. The
record builders set what a fake board reports, for example `board.lights = [light_record(0, pin=2)]`. A fake
board can also be unplugged and plugged back, reconfigured, taken over by another application or have its
animations go idle, and `frames` keeps every frame it published. `attach()` puts fake boards on a fake bus, so
`open_device()`, `find_board()` and the rest find them.

### Conformance checks

`gp2040ce_hostlighting.conformance` holds the checks `hlp-conformance` runs. `run_checks()` runs them under a
`LightsClaim` and yields each `CheckResult` as it is made, and `Checks` collects and counts them.
`measure_round_trips()`, `measure_paced_frames()` and `measure_unthrottled_frames()` take the measurements
`hlp-bench` prints.

## The hlp-* commands

The `hlp-*` tools talk to a board running the GP2040-CE Host Lighting add-on, which exposes a vendor HID
interface for driving the board's RGB LEDs from host software. They speak Host Lighting Protocol 2.0.

* They require the `hidapi` package and a board with the add-on enabled (Configuration -> Add-Ons
  Configuration -> Host Lighting in the web configurator).
* If more than one board is connected, select one with `--board-id <hex prefix of its factory ID>`.
* Actions, light kinds and colour formats are GP2040-CE's own enum values. Given GP2040-CE's `.proto` files
  (`-P`), the tools also print their names.

On Linux, a board's hidraw node is open only to root until a udev rule grants access. The rule below lets the
logged-in user reach the lighting interface in each mode that carries it, at the mode's USB ID. A USB ID
override in the web configurator changes that ID. Put the rule in
`/etc/udev/rules.d/70-gp2040ce-host-lighting.rules`, run `sudo udevadm control --reload-rules` and replug the
board.

```
# Generic HID, Keyboard, SInput, and XInput with Host Lighting
KERNEL=="hidraw*", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="82c0", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="cafe", ATTRS{idProduct}=="0001", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="10c6", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="24c6", ATTRS{idProduct}=="5bad", TAG+="uaccess"
```

* `hlp-ping` says HELLO, checks the magic and protocol version, and times the command round trip.
* `hlp-caps` decodes everything the board reports about itself: the HELLO capabilities, identity, state, input
  modes, summary, input profiles, controls, lights and on-board animations. The state includes the application
  holding the claim and the claim generation. The summary includes the display's button layouts and each
  configuration page's change counter. Controls are listed lit or not. Each light is listed with its LEDs,
  kind, owner and grid cell, and each animation with its speeds.
* `hlp-fill` fills the LEDs with a colour (`--scope all|buttons|case|pleds`) as a quick visual test, then
  restores the board's own animations.
* `hlp-set-light` colours lights by their light ordinal, or with `--by pin|action|kind` by what they belong
  to, then restores the board's own animations. It names the addresses skipped, which have no light or none
  below the LED limit. An eight-digit colour (`RRGGBBWW`) sends RGBW pixels. Put `--` before the entries when
  an address is negative (`--by action -- -10:FF0000`).
* `hlp-stream` streams whole frames as a worked example: red, green and blue fills, then a rainbow at 60 fps.
  Each frame is sent as LED runs with `NO_REPLY`, and with `COMMIT_AFTER` on the last report, which alone is
  answered. `--leds` sets the frame size (default: the LED extent). `--timeout-test` then goes silent and
  times how long the board keeps the frame, which is the keepalive timeout.
* `hlp-buttons` gives every lit control its own hue (staged by action), then every light its own hue (one
  light run), then the buttons the board lights among B1-B4, L1, R1, L2 and R2 white over the running
  animations (overlay takeover).
* `hlp-settings` shows the input profile, animation, animation speeds and brightness with the values each
  accepts, and changes them (`--profile`, `--animation N|off`, `--speed ORDINAL --idle/--pressed/--case`,
  `--brightness`). `--profile` switches the live button mapping. Each differing value is written to the
  board's settings flash shortly after, so use it for a user's choice and not as an effect. `--brightness N
  --unsaved` shows step N unsaved for `--seconds` (default 5) and writes nothing. The command holds the claim
  meanwhile, so `--force` and `--exclusive` apply. The board shows the saved step again when the command ends,
  Ctrl-C and a killed command included.
* `hlp-watch` subscribes to the board's `STATE_CHANGED` event and prints what each one changes: input mode,
  profile, player, animation, the brightness step shown and the saved one, takeover, the application holding
  the claim, whether its claim is exclusive, the claim generation, the `CONFIGURE` settings and the state
  token. The subscription is shared. Every application with the interface open receives the event until the
  board's USB session ends.
* `hlp-input-mode` sets the board's input mode and reboots into it. It accepts the modes the board lists. A
  mode without the lighting interface needs `--force`, because these tools cannot switch the board back from
  one.
* `hlp-reboot-webconfig` / `hlp-reboot-bootsel` reboot the board into the web configurator or the BOOTSEL
  bootloader for flashing.
* `hlp-conformance` checks a board against the protocol and prints each rule it checks. It covers the HELLO
  capabilities, framing, every page and field mask and the pages' cross-references. It covers takeover,
  keepalive and claim-lease timing, the claim generation and the `CONFIGURE` defaults at each change of
  holder, shared and exclusive claims, unsaved brightness, staging, events and board-feature argument checks.
  It checks whether a second handle on the board receives every reply, which is the operating system's part of
  sharing a board, then measures round trip and throughput. It holds the claim while it runs and renews it
  without keeping a takeover alive, so run it with no other lighting application. `--settings` also changes
  and restores brightness, animation and a speed, which writes the settings flash, and checks the change
  counters. `--quick` shortens the timing runs.
* `hlp-bench` measures round trip (median and p95) and frame rates: whole frames paced at 60 fps, then back to
  back with every report answered and pipelined. `--leds` sets the frame size (default: the LED extent).

The tools that drive the LEDs (`hlp-fill`, `hlp-set-light`, `hlp-stream`, `hlp-buttons`, `hlp-bench`,
`hlp-conformance`) claim the board while they do.

* They give the claim up afterwards, Ctrl-C included, which restores the on-board animations and the
  `CONFIGURE` defaults.
* Their requests carry the claim generation. Once another application takes over a tool's claim, the tool is refused at
  once and cannot stage into that application's frame.
* A tool takes its own claim again at its next renewal when it lapsed, on a stalled PC for example, and sends
  its `CONFIGURE` settings again.
* While another application holds the claim, MESH for example, they leave the lights alone unless `--force`
  takes them over.
* Their claim is shared unless `--exclusive` makes it exclusive. Until the lights are handed back, the board
  then refuses changes from applications that do not hold the claim. An `hlp-settings` run alongside is
  refused with `CLAIMED`, naming the holder. Reads, `CLAIM` and `SUBSCRIBE` still work.

Sample usage:

```
% hlp-ping
Haute42 COSMOX M Ultra (v0.7.12-88-gcfe8df1), board ID 433030343237362E
magic GPHL, protocol 2.0, reply queue limit 16
100 round trips in 200 ms (2.0 ms average)

% hlp-caps
...
page 8 (summary): LED extent 46, limit 100, render rate 40 Hz, colour format 0
  brightness maximum 199, 10 steps; animation speed steps 10; grid 19x13
  1 profiles, 21 controls (16 lit, 5 unlit), 46 lights, 4 animations
...
page 10 (controls): 21 (16 lit), token 0x0C37AC27
  GP2 action 1 instance 1 of 2 LED 3 at (9,9), 1 light(s) from ordinal 3
  GP3 action 2 LED 1 at (6,4), 1 light(s) from ordinal 1
  ...
  GP14 action 32 unlit
  ...
  GP27 action 1 instance 2 of 2 LED 12 at (7,2), 1 light(s) from ordinal 12
...

% hlp-settings --brightness 6
profile 1:
  * 1 enabled
animation 0 (an enabled ordinal, or off); speeds 0-10:
  * 0 enabled, idle 1, pressed 0, case 1, speeds 4/4/4
    1 enabled, idle 0, pressed 0, case 0, speeds 4/4/4
  ...
brightness step 6 of 0-10 (maximum 199)

% hlp-settings --brightness 2 --unsaved --exclusive
brightness step 2 shown, unsaved, for 5.0 s
released, on-board animations restored
...
brightness step 6 of 0-10 (maximum 199)

% hlp-watch
   0.000 takeover_live False, brightness_unsaved False, input_mode 0, profile 1, player 1, animation 0, ...
   2.013 animation 0 -> off, brightness_step 5 -> 6

% hlp-conformance
HELLO:
  PASS  protocol major 2
  ...
Takeover and keepalive:
  ...
  PASS  the takeover ends at the timeout
        expiry seen after 302 ms (timeout 300)
...
RESULT: ALL PASS (134 passed, 0 failed)

% hlp-bench
round trip: median 2.00 ms, p95 2.01 ms (300 of 300 answered)
60 fps: 300 of 300 frames acknowledged (46 LEDs, 3 reports per frame)
unthrottled, every report answered: 166 frames/s
unthrottled, pipelined: 249 frames/s
released, on-board animations restored

% hlp-fill --scope case 0000FF
filled case with #0000FF for 3.0 s
released, on-board animations restored

% hlp-set-light --by action 1:FFFFFF00 17:00000080 99:FF0000
2 applied, 1 skipped for 3.0 s
skipped addresses (no light, or none below the LED limit): [99]
released, on-board animations restored
```

The controls page shows that this board wires two buttons to action 1 (Up) and two to action 17 (L3).
`hlp-set-light --by action 1` lights both, and `--by pin` or a light ordinal addresses one.
