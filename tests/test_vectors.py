# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: test_vectors.py
# Folder: tests
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: © 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Test that the test vectors are what scripts/test_vectors.py generates from ModelBoard."""
import importlib.util
import json
import os

from gp2040ce_hostlighting import protocol as hl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_script():
    """Import scripts/test_vectors.py, which is not part of the package."""
    path = os.path.join(ROOT, 'scripts', 'test_vectors.py')
    spec = importlib.util.spec_from_file_location('test_vectors_script', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_vectors_file_is_the_models():
    """Test that test-vectors/vectors.json is the document the generator gives now."""
    with open(os.path.join(ROOT, 'test-vectors', 'vectors.json'), encoding='utf-8') as f:
        assert json.load(f) == load_script().generate()


def test_the_comparison_finds_a_changed_reply_and_a_missing_event():
    """Test that a replay's comparison reports a reply that differs and an event that does not come."""
    script = load_script()
    vector = next(vector for vector in script.generate()['vectors'] if vector['name'] == 'state_changed')
    start = vector['generation_at_start'], vector['takeover_number_at_start']
    assert script.compare(vector, vector['steps'], *start) == []
    refused = [dict(step) for step in vector['steps']]
    reply = bytearray.fromhex(refused[0]['reports'][0])
    reply[2] = 2
    refused[0]['reports'] = [reply.hex()]
    assert script.compare(vector, refused, *start)[0].startswith('  SUBSCRIBE to STATE_CHANGED')
    quiet = [dict(step, reports=[r for r in step['reports'] if not r.startswith('80')]) for step in vector['steps']]
    assert script.compare(vector, quiet, *start)[0].startswith('  events: ')


def test_the_vectors_file_alone_replays_on_the_model():
    """Test that vectors.json alone is enough: its setup, waits, board actions and reads give its reports."""
    script = load_script()
    with open(os.path.join(ROOT, 'test-vectors', 'vectors.json'), encoding='utf-8') as f:
        vectors = json.load(f)['vectors']
    for vector in vectors:
        assert vector['setup'] in (None, 'second_animation'), vector['name']
        model = script.Model(animations=2 if vector['setup'] == 'second_animation' else 1)
        model.recording = False
        model.page1()
        for step in vector['steps']:
            assert step['board_before'] in (None, 'idle_timeout'), (vector['name'], step['label'])
            if step['board_before'] == 'idle_timeout':
                model.set_idle()
            model.sleep(step['wait_before'])
            model.write(bytes.fromhex(step['request']))
            got = [r.hex() for r in model.collect(True, 0)] if step['read'] else []
            assert got == step['reports'], (vector['name'], step['label'])


def test_the_idle_flag_is_compared_only_where_a_vector_shows_it():
    """Test that idle_and_set_animation's comparison finds a changed idle flag, and another vector's does not."""
    script = load_script()
    vectors = {vector['name']: vector for vector in script.generate()['vectors']}
    for name, caught in (('idle_and_set_animation', True), ('claim_echo', False)):
        vector = vectors[name]
        start = vector['generation_at_start'], vector['takeover_number_at_start']
        flipped = []
        for step in vector['steps']:
            reports = []
            for report in step['reports']:
                data = bytearray.fromhex(report)
                if data[0] in (hl.EVENT_MARKER, hl.CMD_GET_PAGE | hl.REPLY_FLAG) and data[3] == hl.PAGE_STATE:
                    data[12] ^= hl.STATE_IDLE
                reports.append(data.hex())
            flipped.append(dict(step, reports=reports))
        assert bool(script.compare(vector, flipped, *start)) is caught, name


def test_a_scenario_that_raises_fails_and_the_board_is_reset(capsys):
    """Test that a replay marks a scenario that raises as failed, resets the board, and goes on."""
    script = load_script()
    vectors = [vector for vector in script.generate()['vectors'] if vector['name'] in ('hello', 'claim_echo')]
    model = script.Model()

    def claims_then_fails(d):
        script.claim(d, 'A claims', hl.CLAIM_TAKE, script.A, script.NAME_A)
        raise TimeoutError('no reply')
    assert script.replay_on(model, vectors, {'hello': script.hello, 'claim_echo': claims_then_fails}) == 1
    out = capsys.readouterr().out
    assert 'hello: same' in out and 'claim_echo: FAIL' in out and 'ended by TimeoutError: no reply' in out
    assert model.board.holder_token == 0
