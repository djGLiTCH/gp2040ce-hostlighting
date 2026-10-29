<!--
gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
Filename: README.md
Folder: tests
https://github.com/djGLiTCH/gp2040ce-hostlighting
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

# tests

The tests, run with pytest from the repository's root after
`pip install -e .[dev]`:

```
python -m pytest
```

They run on the library's fake boards and model board (`testing.py`), so they
need no board. [`vectors/`](vectors/) holds the test vectors.

| File | What it tests |
|---|---|
| `conftest.py` | Not a test: the fixtures, a fake board, the model board and a runner that calls a command against either |
| `test_protocol.py` | The protocol module: framing, the decoders, the errors, and the README's library example |
| `test_device.py` | The device's call for every command, its claim, board discovery and the capability checks |
| `test_claim.py` | `LightsClaim`: taking, renewing, following and giving up the claim |
| `test_frames.py` | `Frame` and `publish_frame()` |
| `test_cache.py` | `ConfigurationCache` |
| `test_session.py` | `Session`, the specification's whole host flow, on the model board's clock, and the README's Session example |
| `test_runner.py` | `SessionRunner`, `AsyncSession`, `BoardWatcher` and `SessionGroup` |
| `test_layout.py` | `lights_by_cell()` and `Canvas` |
| `test_enums.py` | Reading GP2040-CE's enum names from its `.proto` files |
| `test_testing.py` | The fake boards themselves: frames, another application, idle animations, reconfiguring and the bus |
| `test_conformance.py` | `hlp-conformance` and `hlp-bench` against the model board |
| `test_commands.py` | The `hlp-*` commands |
| `test_hlp.py` | The `hlp` command and its menu |
| `test_licence.py` | That the root `LICENSE` is the same text as `LICENSES/GPL-3.0-or-later.txt` |
| `test_vectors.py` | That `vectors/vectors.json` is what `scripts/test_vectors.py` writes from the model board now |
