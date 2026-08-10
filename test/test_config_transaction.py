"""The config read-modify-write transaction (issue #2147).

Every test here is written to fail against the pattern this replaces -- a bare
``read_config_for_update`` followed by ``write_config_atomically`` -- so the suite
demonstrates the defect as well as the fix. ``test_the_defect_is_real`` is the
control: it exercises the OLD pattern and asserts an update IS lost, so a future
refactor that quietly reverts to it cannot leave this file green.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from kiro_crew.config.loader import (
    ConfigBusyError,
    amutate_config,
    config_fingerprint,
    config_transaction,
    mutate_config,
    read_config_for_update,
    write_config_atomically,
)


@pytest.fixture()
def cfg(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    path = home / "config.json"
    path.write_text(json.dumps({"timezone": "UTC"}))
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


class TestTheDefectIsReal:
    """The control. Without the transaction, one writer's change is destroyed.

    Two updaters read the same snapshot, each adds its own key, and the second write
    replaces the first -- the classic lost update. Asserting the bug exists is what
    stops a later refactor from reverting to the old pattern unnoticed.
    """

    def test_the_old_pattern_loses_an_update(self, cfg) -> None:
        both_read = threading.Barrier(2)

        def updater(key: str) -> None:
            raw = read_config_for_update(cfg)
            both_read.wait(timeout=5)  # force the interleaving deterministically
            raw[key] = "set"
            write_config_atomically(cfg, raw)

        a = threading.Thread(target=updater, args=("from_a",))
        b = threading.Thread(target=updater, args=("from_b",))
        a.start()
        b.start()
        a.join(10)
        b.join(10)

        final = _read(cfg)
        survived = [k for k in ("from_a", "from_b") if k in final]
        assert len(survived) == 1, (
            "expected exactly one update to survive, proving the lost-update defect; "
            f"got {survived}"
        )


class TestTheTransactionPreventsIt:
    def test_both_concurrent_updates_survive(self, cfg) -> None:
        """The same interleaving, under the transaction, keeps both changes."""
        started = threading.Barrier(2)

        def updater(key: str) -> None:
            started.wait(timeout=5)  # both threads race for the lock at once

            def apply(raw: dict) -> None:
                raw[key] = "set"
                # Widen the critical section so the loser genuinely has to wait; without
                # a lock this sleep is what guarantees the clobber.
                time.sleep(0.05)

            mutate_config(apply, cfg, timeout=10.0)

        a = threading.Thread(target=updater, args=("from_a",))
        b = threading.Thread(target=updater, args=("from_b",))
        a.start()
        b.start()
        a.join(30)
        b.join(30)

        final = _read(cfg)
        assert final.get("from_a") == "set", "thread A's update was lost"
        assert final.get("from_b") == "set", "thread B's update was lost"
        assert final["timezone"] == "UTC", "the pre-existing value was dropped"

    def test_many_concurrent_updaters_all_land(self, cfg) -> None:
        """Serialisation must hold under more contention than two threads."""
        n = 12
        ready = threading.Barrier(n)

        def updater(i: int) -> None:
            ready.wait(timeout=10)
            mutate_config(lambda raw: raw.__setitem__(f"k{i}", i), cfg, timeout=30.0)

        threads = [threading.Thread(target=updater, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)

        final = _read(cfg)
        missing = [f"k{i}" for i in range(n) if f"k{i}" not in final]
        assert not missing, f"lost updates from {missing}"


class TestTheFingerprintIsContentBased:
    def test_an_equal_length_write_is_detected(self, cfg) -> None:
        """`(mtime, size)` cannot see this; a content hash must.

        Two writes of the same byte length inside one filesystem timestamp tick are
        indistinguishable by stat, which is why the check hashes the bytes.
        """
        cfg.write_text(json.dumps({"v": "aaa"}))
        before = config_fingerprint(cfg)
        cfg.write_text(json.dumps({"v": "bbb"}))  # same length, likely same mtime tick
        assert len(cfg.read_text()) == len(json.dumps({"v": "aaa"}))
        assert config_fingerprint(cfg) != before

    def test_absence_is_not_an_error(self, tmp_path) -> None:
        assert config_fingerprint(tmp_path / "nope.json") is None


class TestTheLockIsASidecar:
    def test_the_lock_file_is_beside_the_config_not_the_config(self, cfg) -> None:
        """A lock on the config inode is released by the rename that replaces it.

        `write_config_atomically` is tmp+rename, so a lock held on the config's own
        inode is dropped by the very write it guards -- the next writer would find the
        lock free while the first was still mid-transaction.
        """
        with config_transaction(cfg) as txn:
            data = txn.read()
            data["x"] = 1
            inode_before = cfg.stat().st_ino
            txn.write(data)
        assert cfg.with_name("config.json.lock").exists()
        assert cfg.stat().st_ino != inode_before, "write should have replaced the inode"

    def test_the_lock_survives_the_rename_it_guards(self, cfg) -> None:
        """Holding the transaction blocks a second acquirer even after the write."""
        acquired_second = []

        def contender() -> None:
            try:
                with config_transaction(cfg, timeout=0.05):
                    acquired_second.append(True)
            except ConfigBusyError:
                acquired_second.append(False)

        with config_transaction(cfg) as txn:
            data = txn.read()
            data["x"] = 1
            txn.write(data)  # the rename happens here, INSIDE the lock
            t = threading.Thread(target=contender)
            t.start()
            t.join(10)

        assert acquired_second == [False], (
            "a second writer got the lock while the first transaction was still open -- "
            "the rename released it, which is the config-inode footgun"
        )


class TestRefusalSemantics:
    def test_a_contended_transaction_raises_rather_than_proceeding(self, cfg) -> None:
        holding = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with config_transaction(cfg):
                holding.set()
                release.wait(timeout=10)

        t = threading.Thread(target=holder)
        t.start()
        assert holding.wait(timeout=10)
        try:
            with pytest.raises(ConfigBusyError):
                with config_transaction(cfg, timeout=0.05):
                    pass
        finally:
            release.set()
            t.join(10)

    def test_required_false_proceeds_unlocked(self, cfg) -> None:
        """For a genuinely optional write, skipping the wait beats blocking.

        `load()`'s one-time migration write-back is the motivating case: it
        re-materialises defaults it already holds, so proceeding (or not) under
        contention loses nothing, while waiting would put a lock wait on every path
        that merely reads config.
        """
        holding = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with config_transaction(cfg):
                holding.set()
                release.wait(timeout=10)

        t = threading.Thread(target=holder)
        t.start()
        assert holding.wait(timeout=10)
        try:
            with config_transaction(cfg, timeout=0.01, required=False) as txn:
                assert txn.locked is False
        finally:
            release.set()
            t.join(10)

    def test_busy_is_an_oserror(self) -> None:
        """Callers already handle OSError from the underlying write; so must this.

        The one behaviour it must never produce is a silent success.
        """
        assert issubclass(ConfigBusyError, OSError)

    def test_writing_without_reading_is_refused(self, cfg) -> None:
        with config_transaction(cfg) as txn:
            with pytest.raises(RuntimeError, match="before read"):
                txn.write({"anything": 1})

    def test_an_intervening_write_inside_the_transaction_is_caught(self, cfg) -> None:
        """Belt and braces: even unlocked, the fingerprint refuses a stale write."""
        with config_transaction(cfg, required=False) as txn:
            data = txn.read()
            data["mine"] = 1
            cfg.write_text(json.dumps({"theirs": 1}))  # simulate a non-participant
            with pytest.raises(ConfigBusyError):
                txn.write(data)
        assert _read(cfg) == {"theirs": 1}, "the other writer's content was clobbered"


class TestTheEventLoopStaysFree:
    def test_amutate_config_does_not_block_the_loop(self, cfg) -> None:
        """The async form must wait on a thread, not on the event loop.

        A lock wait taken inline from a coroutine freezes every session and the liveness
        heartbeat -- the reason this helper exists at all. The loop's own ticks are
        measured while another thread holds the lock: if the wait were on the loop, the
        tick count would collapse to roughly zero.
        """
        holding = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with config_transaction(cfg):
                holding.set()
                release.wait(timeout=10)

        t = threading.Thread(target=holder)
        t.start()
        assert holding.wait(timeout=10)

        async def scenario() -> int:
            ticks = 0

            async def heartbeat() -> None:
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.005)
                    ticks += 1

            beat = asyncio.create_task(heartbeat())
            writer = asyncio.create_task(
                amutate_config(lambda raw: raw.__setitem__("async", 1), cfg, timeout=10.0)
            )
            await asyncio.sleep(0.2)  # loop must keep ticking while the lock is held
            release.set()
            await writer
            beat.cancel()
            return ticks

        try:
            ticks = asyncio.run(scenario())
        finally:
            release.set()
            t.join(10)

        assert ticks > 10, (
            f"event loop ticked only {ticks} times while the config lock was held -- "
            "the wait is happening ON the loop"
        )
        assert _read(cfg)["async"] == 1


class TestUnrelatedSettingsSurvive:
    def test_a_mutation_preserves_every_other_key(self, cfg) -> None:
        cfg.write_text(
            json.dumps({"timezone": "UTC", "agent": {"model": "m"}, "registries": [1, 2]})
        )
        mutate_config(lambda raw: raw.__setitem__("added", True), cfg)
        final = _read(cfg)
        assert final["timezone"] == "UTC"
        assert final["agent"] == {"model": "m"}
        assert final["registries"] == [1, 2]
        assert final["added"] is True
