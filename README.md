<!--
gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
Filename: README.md
Folder: (repository root)
https://github.com/djGLiTCH/gp2040ce-hostlighting
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

<p align="center">
  <img src="packaging/icons/hlp.svg" alt="The hlp icon: a leverless controller's buttons" width="128">
</p>

<h1 align="center">gp2040ce-hostlighting</h1>

<p align="center">
  A Python library and tools for the GP2040-CE Host Lighting add-on, which lets
  host software drive a board's RGB LEDs live over a vendor HID interface.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-GPL--3.0--or--later-blue" alt="Licence: GPL-3.0-or-later"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.14%2B-blue" alt="Python 3.14 or later"></a>
  <a href="https://github.com/OpenStickCommunity/GP2040-CE"><img src="https://img.shields.io/badge/GP2040--CE-TBC-lightgrey" alt="GP2040-CE version: to be confirmed"></a>
</p>

## Installation

```
pip install git+https://github.com/djGLiTCH/gp2040ce-hostlighting@v1.0.0rc3
```

The library goes on PyPI as `gp2040ce-hostlighting` at version 1.0.0, when
Host Lighting Protocol 2.0 is final. Until then each release candidate is a tag
in this repository. pip installs it from the tag, which needs git.

It needs Python 3.14 or later. hidapi has no wheels for Python 3.15 yet, so on
3.15 pip builds it from source, which needs a C compiler, and on Linux the
development packages hidapi's README lists.

From a clone, `pip install -e .[dev]` installs the commands and the development
tools.

The hlp app needs no Python: each release on the repository's Releases page
carries it for Windows (x64), macOS (x64 and ARM64) and Linux (x64 and
ARM64), built by the Release workflow (`.github/workflows/`).

## The hlp app

The hlp app is `hlp` built as a single executable that needs no Python
installed, for Windows, macOS and Linux. Opened from a file manager it shows its
menu; in a terminal it works as `hlp` does.

The app holds this package's licence and the notices of the libraries it
bundles. `hlp --licences` (or `--licenses`), or `l` in the menu, prints them,
and each build also writes them beside the app as `THIRD-PARTY-NOTICES.txt`. On
Linux the libraries the app bundles besides Python and hidapi come from Ubuntu's
packages, and each release also carries the source of those with a GPL-family
licence named in their copyright file, as `hlp-linux-*-sources.tar.gz`.

To build it from a clone, with Python 3.14 or later and git, run the script for
the platform in the repository's root. On Linux the build compiles hidapi
against the system's libudev and libusb, so it also needs a C compiler, binutils
and their development packages (on Debian and Ubuntu: `build-essential
libudev-dev libusb-1.0-0-dev`).

```
powershell -ExecutionPolicy Bypass -File scripts\build-app.ps1
sh scripts/build-app.sh
```

Each makes a virtual environment in `build/app-venv`, installs the versions in
`packaging/requirements.txt` and runs PyInstaller on `packaging/hlp.spec`.
PyInstaller builds for the platform and processor it runs on.

* Windows: `dist\hlp.exe`, with the icon, and the author, copyright and version
  in its Properties. It is not signed, so SmartScreen may warn on the first run.
  Windows 11 on ARM runs it through its x64 emulation.
* macOS: `dist/hlp`, and `dist/hlp.app`, signed ad hoc, which carries the icon
  and opens `hlp` in Terminal. A downloaded copy is not notarised, so macOS
  blocks its first run: open it once, then choose Open Anyway in System
  Settings, Privacy & Security. `xattr -dr com.apple.quarantine hlp.app` in
  Terminal does the same.
* Linux: `dist/hlp`, with `hlp.desktop` and `hlp.png` beside it. To install
  them for the logged-in user (the udev rule below gives access to the board):

```
install -D -m 755 dist/hlp ~/.local/bin/hlp
install -D -m 644 dist/hlp.png ~/.local/share/icons/hicolor/256x256/apps/hlp.png
mkdir -p ~/.local/share/applications
sed "s|^Exec=hlp$|Exec=$HOME/.local/bin/hlp|" dist/hlp.desktop > ~/.local/share/applications/hlp.desktop
```

`scripts/make_icons.py` draws the icon files again; it needs Pillow.

## Layout

Each folder's README says what the folder is for and what each file in it does.

| Folder | Holds |
|---|---|
| [`gp2040ce_hostlighting/`](gp2040ce_hostlighting/) | The library: the protocol, the claim, frames, the session and the test boards |
| [`gp2040ce_hostlighting/tools/`](gp2040ce_hostlighting/tools/) | The `hlp` command and the `hlp-*` commands |
| [`docs/`](docs/) | The Host Lighting specification |
| [`tests/`](tests/) | The tests, run with pytest |
| [`tests/vectors/`](tests/vectors/) | Exact request and reply reports from `ModelBoard`, for hosts with a board model of their own |
| [`scripts/`](scripts/) | Scripts that build the hlp app, draw its icons, and write or replay the test vectors |
| [`packaging/`](packaging/) | What the hlp app is built from: the PyInstaller spec, the pinned build requirements, the third-party notices, the icons and the macOS and Linux files |
| [`.github/workflows/`](.github/workflows/) | The Release workflow, which builds the app on each platform and drafts each release |
| `.github/ISSUE_TEMPLATE/` | The issue forms: bug report, enhancement request and documentation |
| `LICENSES/` | The licence text, `GPL-3.0-or-later.txt`, where REUSE keeps it |

At the root, `LICENSE` is the licence text, `pyproject.toml` describes the
package, its commands and the settings of its checks, `REUSE.toml` gives the
licence of the files that cannot carry a header, `.gitattributes` keeps the
shell scripts' line endings LF, `.gitignore` leaves build output out of git, and
`.git_archival.txt` gives setuptools-scm the version in a GitHub source archive.

## Specification

`docs/host-lighting.md` is the Host Lighting Protocol 2.0 specification, the
document the Host Lighting add-on adds to GP2040-CE as `docs/host-lighting.md`.

## Host implementations

Examples of hosts that speak the protocol:

* [hlp-spice2x](https://github.com/djGLiTCH/hlp-spice2x) shows the cabinet
  lighting of games running under spice2x on a GP2040-CE controller's buttons.
  It is built on this library.
* MESH (Modern Emulator State Hub) is a cross-platform desktop app that drives
  controller lighting from emulator game state. It implements the protocol
  itself, with this library as the reference for host behaviour, and replays
  `tests/vectors/` against its own board model.

## The library

`gp2040ce_hostlighting.protocol` speaks Host Lighting Protocol 2.0 to a board.
It needs only the `hidapi` package. The library never prints, exits or sets up
logging. Each module logs to its own logger. Each protocol or device failure
raises a subclass of `HostLightingError`, and an invalid argument raises
`ValueError`. A device, and everything built on it, belongs to one thread. Each
call that sends an answered request blocks for its round trip, and for about
200 ms while the board writes its settings flash. The package carries type
hints (`py.typed`) and passes mypy's strict mode.

A host drives the lights in these steps:

1. Open a board with `open_device()`. With several boards connected, pass a hex
   prefix of the board ID. When it finds no board to open, it raises
   `HostLightingBoardSelectionError`. Its `boards` and `matched` name the
   boards it found and `interfaces` counts the interfaces found, answering or
   not. `find_board()` opens one board by its whole ID and returns `None` when
   it is not connected.
2. Read what the board reports with `hello()`, `read_identity()`,
   `read_record()` and `read_table()`. The two reads give a `PageRead` of the
   records and the state token. Each record carries its `ordinal`, its position
   in the page, and one attribute per field group, named after the
   specification's, such as `light.owner.pin`. Reading a group the reply did
   not return raises `HostLightingGroupNotReadError`, and `record.has(group)`
   says whether it was returned. A page reply at a record stride this
   library's layout cannot have raises `HostLightingIncompatibleError`, naming
   the page. A type checker knows each page's record type, such as
   `LightRecord` for `read_table(PAGE_LIGHTS)`. A host without a `Session`
   checks the HELLO reply with `require_capabilities()`.
3. Take the claim with `LightsClaim(device, name, settings).take()`, where the
   name is up to 16 bytes. `take()` claims, then sends the CONFIGURE settings,
   and drops the events received by then, which are older than the claim.
   While another application holds the claim, or takes it over before the
   settings are applied, it raises `HostLightingClaimedError`, unless it is to
   take the claim over (`force=True`) or wait for it (`wait=True`).
4. Stage pixels with `stage_run()` or `stage_entries()`, and publish them with
   `commit=True`, only while `claim.state` is `HELD`. Pass a status 4
   `HostLightingRejectedError` from staging to `claim.refused()` before staging
   again. After a refusal that shows another holder, the device sends no
   generation, so a frame would land in that application's claim.
5. Call the claim's `maintain()` often. It renews the claim when a renewal is
   due and returns a `ClaimChange`. When the change's `restage` is true, the
   claim started again with nothing staged, so stage the whole frame again.
   `keep(seconds, restage)` maintains the claim for a while, sleeping between
   renewals, and calls `restage` when a renewal took the claim again.
6. Give the claim up with `give_up()`, which a `with` block does, then close
   the device.

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

`LightsClaim` logs each change at INFO. Once another application takes the
lights over, `maintain()` returns a change of kind `LOST` and `keep()` returns
`False`. The claim then waits, and each renewal asks for it until the board is
unclaimed. With `wait_when_taken_over=False` the claim ends there, and the
lights belong to the new holder. `observe()` applies the holders that
`STATE_CHANGED` events show, and `refused()` applies a status 4 refusal to the
claim.

A streaming host builds each frame as a `Frame` of entries and runs, staged in
the order they were added, and sends it with `publish_frame()`. Every report
but the last goes with `NO_REPLY`, and the last with `COMMIT_AFTER`, which
alone is answered and is sent once more when its answer does not come. The
`FrameResult` says whether the frame was acknowledged, and is true when it was.
When it was not, a host sends `HELLO` before taking the board as lost, and
passes a status 4 reply to `claim.refused()`, as `Session.show()` does.

The board refuses an LED run that reaches past the Summary's `led_limit`, and
in a report sent with `NO_REPLY` the refusal is silent. `publish_frame()` with
`led_limit=`, and `Session.show()` always, raise `ValueError` for such a run
before anything is sent. `Frame.check_led_limit()` makes the same check.

A `ConfigurationCache` keeps the configuration pages. Its `refresh()` reads the
Summary, then each page with a moved change counter, under the Summary's state
token, and returns the pages it read again. Its `moved` names every page with a
moved counter, kept or not. A host calls it after each change of the state
token, and calls `clear()` after a reconnect. `lights_reached()` gives the
lights a `STAGE` entry reaches, as the board resolves it: by ordinal, pin,
action or kind, leaving out a light with no LED below the addressable limit.
`controls_with()` gives the controls with a key or an action, and
`keyboard_code()` the code the Keyboard input mode sends for an action's
button, from page 11. Up to Right on a GPIO pin or Hall-effect trigger send
their codes only in the digital D-pad mode (page 1). The Summary's
`brightness_scale()` gives the scale the board applies at a brightness step,
and `stream_rate()` the render rate, past which frames are not shown.

A `Session` runs the specification's whole host flow on these parts. `start()`,
or a `with` block, checks the board's capabilities, reads the configuration,
takes the claim, sends the settings and subscribes to `STATE_CHANGED`. `poll()`
renews the claim, applies the events, reads changed pages again and returns a
`SessionUpdate`. Its `restage` says when to stage the whole frame again, and
its `changed` names the page 1 fields that changed since the last update.
`show()` publishes a frame, the first after each start clearing first.
`configure()` changes the settings, `reconnect()` finds the board again after
it leaves the bus, and `close()` gives the claim up. Before `start()`, `poll()`
and `show()` raise `HostLightingError`. A failed start in a `with` block closes
the device. Its cache keeps the pages and groups that `pages` and `masks` name,
by default all of them. With the name `None`, a `Session` runs the flow of a
host that does not claim. It sends its settings only while the board is
unclaimed, and the defaults only in place of its own, while page 1 shows them
and no other since. Its `close()` puts the defaults back in place of its own
settings likewise, and once it has sent a frame, sends `RELEASE` and
`SET_BRIGHTNESS` 0xFF.

The example shows the case lights blue for 3 s at 60 frames a second:

```python
import time

from gp2040ce_hostlighting.frames import Frame
from gp2040ce_hostlighting.protocol import (
    ADDRESSING_KIND, KIND_CASE, open_device)
from gp2040ce_hostlighting.session import Session

with Session(open_device(), 'my-host') as session:
    frame = Frame()
    frame.entries(ADDRESSING_KIND, [(KIND_CASE, (0, 0, 255))])
    start = time.monotonic()
    for index in range(180):
        session.poll()
        session.show(frame)
        time.sleep(max(0.0, start + (index + 1) / 60 - time.monotonic()))
```

The loop sleeps to each frame's time rather than for a fixed 1/60 s, so the
time spent sending a frame does not slow the rate.

A device belongs to one thread and every call on it blocks. A `SessionRunner`
gives a session a thread that owns its device, for a GUI or any program that
must not block. `show()` posts the latest frame and `configure()` new settings
from any thread. The runner shows the frame posted last, again whenever an
update asks for a restage, and finds the board again by its board ID after it
leaves the bus. Each update that carries anything goes to `on_update`, called
on the runner's thread, and to the `updates` queue. `AsyncSession` puts a
runner behind awaitables for an `asyncio` program: `await update()`, or
`async for`, which ends when the runner is closed and raises the error that
ended it. A runner keeps the frame posted, so a host posts a new `Frame` for
each change, and closes each runner before it exits. A frame past the board's
LED limit when the runner shows it, as one held for a board back with a lower
limit, is dropped with a warning.

`BoardWatcher` reports boards arriving and leaving by board ID, opening only an
interface it has not seen. `SessionGroup` keeps a runner for each board on the
bus, built by a function the host gives. A board that fails to start a session
is tried again after `retry_seconds`, the wait doubling to `retry_max_seconds`,
with one warning until a start succeeds.

`lights_by_cell()` maps each cell of the LED layout grid to its lights. A
`Canvas` of the grid's size, `Canvas.for_summary(summary)`, holds a colour per
cell, and its `frame()` colours each light with its cell's colour.

`jsonable()` in `records.py` turns any record, field group or HELLO into
values `json.dumps()` takes. The logger `gp2040ce_hostlighting.trace` logs each
report written (`>`) and read (`<`) in hex at DEBUG, the commands' `-d`
included. `hlp-caps --json` and `hlp-conformance --json` print JSON on stdout.

A host that sends with `send()` and waits with `wait_reply()` reads the reports
already received with `drain()` before each send, takes the events received
with `take_events()`, and builds the error for a refused reply with
`HostLightingDevice.rejected()`. The error's `holder` is the holder as
`claim()` returns it. A device keeps up to `EVENTS_KEPT` events until they are
taken, the oldest dropped first.

### What a session recovers from

A `Session` keeps to the claim rules through these failures:

* Another application takes the claim, shared or exclusive, at any point,
  `start()` included. A host that claims waits, or with
  `wait_when_taken_over=False` stops, and sends no frame and no `CONFIGURE`
  into that application's claim. One that does not claim stops at its next
  page 1 read or event.
* A request or its reply is lost. The call raises
  `HostLightingTimeoutError`, and `poll()` raises
  `HostLightingUnansweredError` after three in a row. A frame without an
  acknowledgement is sent once more, then `HELLO` follows. A `CLAIM` without
  a reply may have taken the claim, so `take()` and `close()` give it up. A
  claim renews at once and sends the settings when its `CONFIGURE`, or the
  renewal before a `configure()` change, goes unanswered. A host that does not
  claim reads page 1 before each `CONFIGURE`, counts one without a reply as
  applied once page 1 shows its settings or another host's sent since, and
  sends it again while page 1 still shows what it showed before. When the
  read before it goes unanswered, settings other than the defaults go over
  the settings page 1 showed only after a change of holder, or as the resend
  of an earlier `CONFIGURE` that did not land, so a caller that needs them
  now should call `configure()` again.
* An event is lost. Every session reads page 1 at least every second, or a
  third of the timeout when that is shorter, with events or without, and at
  the next `poll()` after a read goes unanswered, so a change of holder or of
  the state token is learned.
* The board leaves the bus. `reconnect()` finds it again by its board ID,
  starts again under the same token, and clears before its first frame.

Some things it cannot rule out. A host that does not claim can make neither
`CONFIGURE` nor a frame conditional, so a claim taken since its last page 1
read or event receives them until its next answered read or event. Two hosts
that do not claim cannot tell settings of the same values apart: the first to
close, or to go back to the defaults, can put the defaults over the other's,
and a resend can go over the other's later settings when those equal what
page 1 showed before. A host that needs known settings claims. The claim
generation has seven values, so a claim that changes holder seven times
between two looks seems unchanged. If a board's USB session restarts without
leaving the bus, it is noticed only when a request fails. `close()` does not
retry a request it loses, so the keepalive timeout or the next change of
holder restores what it would have. A keepalive timeout shorter than the
0.5 s a request waits for its reply lets the claim lapse whenever a reply is
lost.

### Enum names

The pages carry GP2040-CE's enum values verbatim, such as an action or a light
type. `read_enum_names()` in `gp2040ce_hostlighting.enums` reads their names
from GP2040-CE's `enums.proto`, given as the file or its directory, so a host
can show `BUTTON_PRESS_UP` beside action 1. The board gives the names of its
own actions and light kinds on page 3, and `HostLightingDevice.read_names()`
reads them in the same shape, so a host needs no `.proto` file for those.
Page 3 is fixed for the firmware and outside the state token, so
`read_names()` reads it without one, and a host can keep the names by
firmware version. The commands read page 3 only with `--names`, when `-P`
names nothing. The library keeps no list of its own.

### Testing a host

`gp2040ce_hostlighting.testing` fakes a board, so a host's tests run without
one. `fake_device(FakeBoard())` returns a `HostLightingDevice` connected to a
board that answers and checks each request as the firmware does. `ModelBoard`
also follows the protocol's timing, the claim's refusals, events and the change
counters, on a clock the test controls with `sleep()` and `time()`. The record
builders set what a fake board reports, for example
`board.lights = [light_record(0, pin=2)]`. A fake board can also be unplugged
and plugged back, reconfigured, taken over by another application or have its
animations go idle, and `frames` keeps every frame it published.
`attach(monkeypatch, *boards)` puts fake boards on a fake bus, so
`open_device()`, `find_board()` and the rest find them, and pytest's
`monkeypatch` fixture takes them off after the test.

A fake board's `HELLO` comes from its attributes: `magic`, `version`,
`request_flags`, `commands`, `pages`, `addressing_modes`, `pixel_formats` and
`events`, by default what the firmware lists. A command or page left out
answers as one the firmware lacks. `drop` discards the commands it names,
neither run nor answered, and `reject` answers each it names with a status and
runs nothing. Both take a command or a (command, `[3]`) pair and can change
mid-test. Each of `hooks` is called with the board and the request before every
request. Setting `summary` to `None` derives the Summary from the pages.

A host that keeps a board model of its own can test it against
`tests/vectors/vectors.json`: exact requests, and every report `ModelBoard`
sent after each. Its README gives the format, the codes a replay acts on and
the bytes a board's own state changes. `scripts/test_vectors.py --board ID`
replays them on a board.

### Conformance checks

`gp2040ce_hostlighting.conformance` holds the checks `hlp-conformance` runs.
`run_checks()` runs them under a `LightsClaim` and yields each `CheckResult` as
it is made, and `Checks` collects and counts them. `measure_round_trips()`,
`measure_paced_frames()` and `measure_unthrottled_frames()` take the
measurements `hlp-bench` prints.

## The hlp-* commands

The `hlp-*` tools talk to a board running the GP2040-CE Host Lighting add-on,
which exposes a vendor HID interface for driving the board's RGB LEDs from host
software. They speak Host Lighting Protocol 2.0. `hlp` runs each one as a
subcommand: `hlp ping` runs `hlp-ping`, and `hlp --help` lists them. With no
arguments at a console, as when opened from a file manager, `hlp` offers a menu:
it picks the board, then takes a command by number, by name or as a whole
command line and runs it on that board, until `q`. A command that needs
arguments shows its usage and an example first, and the menu asks before
`input-mode` and `reboot`, which restart the board.

* They require the `hidapi` package and a board with the add-on enabled
  (Configuration -> Add-Ons Configuration -> Host Lighting in the web
  configurator).
* If more than one board is connected, select one with
  `--board-id <hex prefix of its board ID>`.
* Actions, light kinds and colour formats are GP2040-CE's own enum values.
  Given GP2040-CE's `.proto` files (`-P`), the tools also print their names.
* `hlp --licences` (or `--licenses`), or `l` in the menu, prints the licence
  and the notices of the libraries the hlp app bundles.

On Linux, the library uses hidapi's hidraw backend, which several applications
can share. A board's hidraw node is open only to root until a udev rule grants
access. The rule below lets the logged-in user reach the lighting interface in
each mode that carries it, at the mode's USB ID. A USB ID override in the web
configurator changes that ID. Put the rule in
`/etc/udev/rules.d/70-gp2040ce-host-lighting.rules`, run
`sudo udevadm control --reload-rules` and replug the board.

```
# Generic HID, Keyboard, SInput, and XInput with Host Lighting
KERNEL=="hidraw*", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="82c0", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="cafe", ATTRS{idProduct}=="0001", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="10c6", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="24c6", ATTRS{idProduct}=="5bad", TAG+="uaccess"
```

* `hlp-ping` says HELLO, checks the magic and protocol version, and times the
  command round trip.
* `hlp-caps` decodes everything the board reports about itself: the HELLO
  capabilities, identity, state, input modes, summary, input profiles,
  controls, keyboard mapping, lights and on-board animations. The state
  includes the D-pad mode, the application holding the claim and the claim
  generation. The summary includes the display's button layouts and each
  configuration page's change counter.
  Controls are listed lit or not. Each light is listed with its LEDs, kind,
  owner and grid cell, and each animation with its speeds.
* `hlp-fill` fills the LEDs with a colour
  (`--scope all|buttons|turbo|case|players`) as a quick visual test, then
  restores the board's own animations.
* `hlp-set-light` colours lights by their light ordinal, or with
  `--by pin|action|kind` by what they belong to, then restores the board's own
  animations. It names the addresses skipped, which have no light or none below
  the LED limit. An eight-digit colour (`RRGGBBWW`) sends RGBW pixels. A
  negative address needs no `--` (`--by action -10:FF0000`).
* `hlp-stream` streams whole frames as a worked example: red, green and blue
  fills, then a rainbow at 60 fps. Each frame is sent as LED runs with
  `NO_REPLY`, and with `COMMIT_AFTER` on the last report, which alone is
  answered. `--leds` sets the frame size (default: the LED extent).
  `--timeout-test` then goes silent and times how long the board keeps the
  frame, which is the keepalive timeout.
* `hlp-show` runs a light show at 60 fps as a visual test, from the lights on
  page 12: a rainbow chase across the buttons, left to right, a rainbow wave
  along the case lights, and the player and turbo lights pulsing cyan.
  `--seconds` sets how long (default: 15). A board that reports no lights shows
  a rainbow along its LEDs.
* `hlp-buttons` gives every lit control its own hue (staged by action), then
  every light its own hue (one light run), then the buttons the board lights
  among B1-B4, L1, R1, L2 and R2 white over the running animations (overlay
  takeover).
* `hlp-settings` shows the input profile, animation, animation speeds,
  brightness and the active profile's keyboard codes with the values each
  accepts, with the D-pad mode the codes of Up to Right depend on, and changes
  them (`--profile`, `--animation N|off`,
  `--speed ORDINAL --idle/--pressed/--case`, `--brightness`,
  `--keyboard-code ORDINAL=CODE`, repeated as needed, consecutive ordinals
  sent in one request). `--profile` switches the live button mapping. Each
  differing value is written to the board's settings flash, a profile or
  keyboard code at once and the others about a second later, so use it for a
  user's choice and not as an effect. `--brightness N --unsaved` shows step N
  unsaved for `--seconds` (default 5) and writes nothing. The command holds the
  claim meanwhile, so `--force` and `--exclusive` apply. The board shows the
  saved step again when the command ends, Ctrl-C and a killed command included.
* `hlp-watch` subscribes to the board's `STATE_CHANGED` event and prints what
  each one changes: input mode, profile, player, animation, the brightness step
  shown and the saved one, the D-pad mode, takeover, the application holding
  the claim, whether its claim is exclusive, the claim generation, the
  `CONFIGURE` settings and the state token. The subscription is shared. Every
  application with the interface open receives the event until the board's USB
  session ends.
* `hlp-input-mode` sets the board's input mode and reboots into it. It accepts
  the modes the board lists. A mode without the lighting interface needs
  `--force`, because these tools cannot switch the board back from one.
* `hlp-reboot gamepad|webconfig|bootsel` reboots the board into its saved input
  mode, the web configurator or the BOOTSEL bootloader for flashing. The saved
  input mode can differ from the one the board runs when a boot hotkey chose
  that one.
* `hlp-conformance` checks a board against the protocol and prints each rule it
  checks. It covers the HELLO capabilities, framing, every page and field mask
  and the pages' cross-references. It covers takeover, keepalive and
  claim-lease timing, the claim generation and the `CONFIGURE` defaults at each
  change of holder, shared and exclusive claims, unsaved brightness, staging,
  events and board-feature argument checks. It checks whether a second handle
  on the board receives every reply, which is the operating system's part of
  sharing a board, then measures round trip and throughput. It holds the claim
  while it runs and renews it without keeping a takeover alive, so run it with
  no other lighting application. `--settings` also changes and restores a
  keyboard code, brightness, animation and a speed, which writes the settings
  flash, and checks the change counters. `--quick` shortens the timing runs.
* `hlp-bench` measures round trip (median and p95) and frame rates: whole
  frames paced at 60 fps, then back to back with every report answered and
  pipelined. `--leds` sets the frame size (default: the LED extent).

The tools that drive the LEDs (`hlp-fill`, `hlp-set-light`, `hlp-stream`,
`hlp-show`, `hlp-buttons`, `hlp-bench`, `hlp-conformance`) claim the board while
they do.

* They give the claim up afterwards, Ctrl-C included, which restores the
  on-board animations and the `CONFIGURE` defaults.
* Their requests carry the claim generation. Once another application takes
  over a tool's claim, the tool is refused at once and cannot stage into that
  application's frame. `hlp-fill` and `hlp-set-light` then end their hold at
  the next renewal and name the new holder.
* A tool takes its own claim again at its next renewal when it lapsed, on a
  stalled PC for example, and sends its `CONFIGURE` settings again.
* While another application holds the claim, MESH for example, they leave the
  lights alone unless `--force` takes them over.
* Their claim is shared unless `--exclusive` makes it exclusive. Until the
  lights are handed back, the board then refuses changes from applications that
  do not hold the claim. An `hlp-settings` run alongside is refused with
  `CLAIMED`, naming the holder. Reads, `CLAIM` and `SUBSCRIBE` still work.

Sample usage:

```
% hlp-ping
Haute42 COSMOX M Ultra (v0.7.12-147-g6650cb8), board ID 433030343237362E
magic GPHL, protocol 2.0, reply queue limit 16
100 round trips in 200 ms (2.0 ms average)

% hlp-caps
...
page 8 (summary): LED extent 46, limit 100, render rate 40 Hz, colour format 0, no white channel
  brightness maximum 199, 10 steps; animation speed steps 10; grid 19x13
  1 profiles, 21 controls (16 lit, 5 unlit), 46 lights, 4 animations
  button layouts 27 and 31, orientation 0
  changes counted on pages 8: 0, 9: 0, 10: 0, 11: 0, 12: 0, 13: 0, 14: 0, 15: 0
...
page 10 (controls): 21 (16 lit), token 0x210EF306
  GP2 action 1 instance 1 of 2 LED 3 at (9,9), 1 light(s) from ordinal 3
  GP3 action 2 LED 1 at (6,4), 1 light(s) from ordinal 1
  ...
  GP14 action 32 unlit
  ...
  GP27 action 1 instance 2 of 2 LED 12 at (7,2), 1 light(s) from ordinal 12
page 11 (keyboard mapping): 32, token 0x210EF306
   0 action 1: code 0x52
  ...
  17 action 16: code 0x3B
  18 action 41: no code
  ...
...

% hlp-settings
profile 1:
  * 1 enabled
animation 0 (an enabled ordinal, or off); speeds 0-10:
  * 0 enabled, idle 1, pressed 0, case 1, speeds 7/3/4
    1 enabled, idle 0, pressed 0, case 0, speeds 4/4/4
  ...
brightness step 4 of 0-10 (maximum 199)
D-pad mode 0 (Up to Right on a GPIO pin or Hall-effect trigger send their codes only in the digital D-pad mode)
keyboard codes, the active profile's (page 11 ordinal, its button's action, code):
   0 action 1: code 0x52
  ...
   4 action 5: code 0xE1
  ...

% hlp-settings --keyboard-code 4=0x68
...
   4 action 5: code 0x68
...

% hlp-settings --brightness 2 --unsaved --exclusive
brightness step 2 shown, unsaved, for 5.0 s
claim given up, on-board animations restored
...
brightness step 4 of 0-10 (maximum 199)
...

% hlp-watch --seconds 8
   0.000 takeover_live False, brightness_unsaved False, animations_idle False, input_mode 0, profile 1, ...
   0.958 holder_token 0x00000000 -> 0xAE7287C7, application_name  -> hlp-settings, exclusive False -> True, ...
   0.966 brightness_unsaved False -> True, brightness_step_shown 4 -> 2
   5.964 brightness_unsaved True -> False, brightness_step_shown 2 -> 4, holder_token 0xAE7287C7 -> 0x00000000, ...

% hlp-conformance --settings
HELLO:
  PASS  protocol major 2
  ...
Takeover and keepalive:
  ...
  PASS  the takeover ends at the timeout
        expiry seen after 310 ms (timeout 300)
...
RESULT: ALL PASS (200 passed, 0 failed)

% hlp-bench
round trip: median 2.00 ms, p95 2.06 ms (300 of 300 answered)
60 fps: 300 of 300 frames acknowledged (46 LEDs, 3 reports per frame)
unthrottled, every report answered: 166 frames/s
unthrottled, pipelined: 249 frames/s
claim given up, on-board animations restored

% hlp-fill --scope case 0000FF
filling case with #0000FF for 3.0 s
claim given up, on-board animations restored

% hlp-set-light --by action 1:FFFFFF00 17:00000080 99:FF0000
2 applied, 1 skipped, holding for 3.0 s
skipped addresses (no light, or none below the LED limit): [99]
claim given up, on-board animations restored
```

The controls page shows that this board wires two buttons to action 1 (Up) and
two to action 17 (L3). `hlp-set-light --by action 1` lights both, and
`--by pin` or a light ordinal addresses one.

## Licence

GPL-3.0-or-later, (C) 2026 Jacob Simpson. Each file carries its SPDX header,
and `REUSE.toml` covers the files that cannot. The licence text is the FSF's,
in `LICENSE` and, where REUSE keeps it, `LICENSES/GPL-3.0-or-later.txt`.
