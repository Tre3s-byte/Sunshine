"""Tests for the StateMachine."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from daemon.core import VALID_STATES, StateMachine


class TestStateMachine:
    def test_initial_state_is_idle(self, tmp_state_path: Path, silent_logger):
        sm = StateMachine(tmp_state_path, silent_logger)
        assert sm.get()["state"] == "IDLE"
        assert sm.get()["generation"] == 0

    def test_transition_persists_to_disk(self, tmp_state_path: Path, silent_logger):
        sm = StateMachine(tmp_state_path, silent_logger)
        sm.transition("STREAMING")
        assert tmp_state_path.exists()
        data = json.loads(tmp_state_path.read_text())
        assert data["state"] == "STREAMING"

    def test_transition_increments_generation(self, tmp_state_path: Path, silent_logger):
        sm = StateMachine(tmp_state_path, silent_logger)
        sm.transition("STARTING")
        sm.transition("STREAMING")
        assert sm.get()["generation"] == 2

    def test_self_transition_is_noop(self, tmp_state_path: Path, silent_logger):
        sm = StateMachine(tmp_state_path, silent_logger)
        sm.transition("STARTING")
        gen_before = sm.get()["generation"]
        sm.transition("STARTING")
        assert sm.get()["generation"] == gen_before

    def test_invalid_state_raises(self, tmp_state_path: Path, silent_logger):
        sm = StateMachine(tmp_state_path, silent_logger)
        with pytest.raises(ValueError):
            sm.transition("BOGUS_STATE")

    def test_restore_from_disk(self, tmp_state_path: Path, silent_logger):
        tmp_state_path.write_text(json.dumps(
            {"state": "GRACE_PERIOD", "generation": 7, "timestamp": 0}
        ))
        sm = StateMachine(tmp_state_path, silent_logger)
        assert sm.get()["state"] == "GRACE_PERIOD"
        assert sm.get()["generation"] == 7

    def test_restore_corrupt_falls_back_to_idle(self, tmp_state_path: Path, silent_logger):
        tmp_state_path.write_text("not-json{{{")
        sm = StateMachine(tmp_state_path, silent_logger)
        assert sm.get()["state"] == "IDLE"
        assert sm.get()["generation"] == 0

    def test_restore_unknown_state_falls_back_to_idle(self, tmp_state_path: Path, silent_logger):
        tmp_state_path.write_text(json.dumps(
            {"state": "ALIENS", "generation": 5, "timestamp": 0}
        ))
        sm = StateMachine(tmp_state_path, silent_logger)
        assert sm.get()["state"] == "IDLE"

    def test_all_documented_states_are_reachable(self, tmp_state_path: Path, silent_logger):
        sm = StateMachine(tmp_state_path, silent_logger)
        for state in VALID_STATES:
            if state == "IDLE":
                continue
            sm.transition(state)
            assert sm.get()["state"] == state
