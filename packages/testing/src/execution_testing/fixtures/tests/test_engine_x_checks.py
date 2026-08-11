"""Tests for the Engine X execution-consistency check."""

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from execution_testing.fixtures.engine_x_checks import (
    ENGINE_X_FIXTURES_DIR,
    SIBLING_FIXTURES_DIR,
    EngineXExecutionDriftError,
    verify_engine_x_execution,
)

ENGINE_X_ID = (
    "tests/a.py::test_a[fork_Prague-blockchain_test_engine_x_from_state_test]"
)
SIBLING_ID = (
    "tests/a.py::test_a[fork_Prague-blockchain_test_engine_from_state_test]"
)


def _payload(
    *, gas_used: str, state_root: str, block_hash: str
) -> Dict[str, Any]:
    """Build a single newPayload entry."""
    return {
        "newPayloadVersion": "4",
        "forkchoiceUpdatedVersion": "3",
        "params": [
            {
                "parentHash": f"0x{'00' * 31}aa",
                "stateRoot": state_root,
                "blockHash": block_hash,
                "gasUsed": gas_used,
                "receiptsRoot": f"0x{'11' * 32}",
                "logsBloom": f"0x{'00' * 256}",
                "transactions": ["0xf86b..."],
            },
            [],
            f"0x{'00' * 32}",
        ],
    }


def _write_fixture(
    folder: Path,
    fixture_dir: str,
    test_id: str,
    payloads: List[Dict[str, Any]],
    sync_payload: Dict[str, Any] | None = None,
) -> None:
    """Write a single-fixture file into a format tree."""
    file = folder / fixture_dir / "prague" / "module" / "test_a.json"
    file.parent.mkdir(parents=True, exist_ok=True)
    fixture: Dict[str, Any] = {"engineNewPayloads": payloads}
    if sync_payload is not None:
        fixture["syncPayload"] = sync_payload
    file.write_text(json.dumps({test_id: fixture}))


def test_identical_execution_passes(tmp_path: Path) -> None:
    """State-root-derived differences alone do not trip the check."""
    _write_fixture(
        tmp_path,
        SIBLING_FIXTURES_DIR,
        SIBLING_ID,
        [_payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")],
    )
    _write_fixture(
        tmp_path,
        ENGINE_X_FIXTURES_DIR,
        ENGINE_X_ID,
        [_payload(gas_used="0x5208", state_root="0xaa", block_hash="0xbb")],
    )

    result = verify_engine_x_execution(tmp_path)

    assert result is not None
    assert result.compared == 1
    assert "1 Engine X fixtures execute identically" in result.summary


def test_execution_drift_raises(tmp_path: Path) -> None:
    """A gas difference (a leaked account changed execution) fails loudly."""
    _write_fixture(
        tmp_path,
        SIBLING_FIXTURES_DIR,
        SIBLING_ID,
        [_payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")],
    )
    _write_fixture(
        tmp_path,
        ENGINE_X_FIXTURES_DIR,
        ENGINE_X_ID,
        [_payload(gas_used="0xbeef", state_root="0xaa", block_hash="0xbb")],
    )

    with pytest.raises(EngineXExecutionDriftError) as exc_info:
        verify_engine_x_execution(tmp_path)

    message = str(exc_info.value)
    assert ENGINE_X_ID in message
    assert "gasUsed" in message


def test_payload_count_drift_raises(tmp_path: Path) -> None:
    """A different number of payloads fails loudly."""
    payload = _payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")
    _write_fixture(tmp_path, SIBLING_FIXTURES_DIR, SIBLING_ID, [payload])
    _write_fixture(
        tmp_path, ENGINE_X_FIXTURES_DIR, ENGINE_X_ID, [payload, payload]
    )

    with pytest.raises(EngineXExecutionDriftError) as exc_info:
        verify_engine_x_execution(tmp_path)

    assert "payload count" in str(exc_info.value)


def test_no_sibling_fixtures_skips_check(tmp_path: Path) -> None:
    """An Engine X only fill (no sibling format tree) skips the check."""
    _write_fixture(
        tmp_path,
        ENGINE_X_FIXTURES_DIR,
        ENGINE_X_ID,
        [_payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")],
    )

    assert verify_engine_x_execution(tmp_path) is None


def test_no_engine_x_fixtures_skips_check(tmp_path: Path) -> None:
    """A fill without Engine X fixtures skips the check."""
    _write_fixture(
        tmp_path,
        SIBLING_FIXTURES_DIR,
        SIBLING_ID,
        [_payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")],
    )

    assert verify_engine_x_execution(tmp_path) is None


def test_single_fixture_per_file_sibling_lookup(tmp_path: Path) -> None:
    """
    A `--single-fixture-per-file` fill embeds the fixture format name in
    every file name; the sibling is still found under its own basename.
    """
    payload = _payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")
    sibling_file = (
        tmp_path
        / SIBLING_FIXTURES_DIR
        / "prague"
        / "module"
        / "a__fork_Prague_blockchain_test_engine_from_state_test.json"
    )
    sibling_file.parent.mkdir(parents=True, exist_ok=True)
    sibling_file.write_text(
        json.dumps({SIBLING_ID: {"engineNewPayloads": [payload]}})
    )
    engine_x_file = (
        tmp_path
        / ENGINE_X_FIXTURES_DIR
        / "prague"
        / "module"
        / "a__fork_Prague_blockchain_test_engine_x_from_state_test.json"
    )
    engine_x_file.parent.mkdir(parents=True, exist_ok=True)
    engine_x_file.write_text(
        json.dumps({ENGINE_X_ID: {"engineNewPayloads": [payload]}})
    )

    result = verify_engine_x_execution(tmp_path)

    assert result is not None
    assert result.compared == 1
    assert result.skipped == 0


def test_missing_sibling_fixture_is_skipped(tmp_path: Path) -> None:
    """A test filtered from the sibling format is skipped, not failed."""
    payload = _payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")
    _write_fixture(tmp_path, SIBLING_FIXTURES_DIR, SIBLING_ID, [payload])
    other_engine_x_id = ENGINE_X_ID.replace("test_a[", "test_b[")
    _write_fixture(tmp_path, ENGINE_X_FIXTURES_DIR, ENGINE_X_ID, [payload])
    file = (
        tmp_path / ENGINE_X_FIXTURES_DIR / "prague" / "module" / "test_b.json"
    )
    file.write_text(
        json.dumps({other_engine_x_id: {"engineNewPayloads": [payload]}})
    )

    result = verify_engine_x_execution(tmp_path)

    assert result is not None
    assert result.compared == 1
    assert result.skipped == 1
    assert "1 skipped" in result.summary


def test_no_matching_siblings_reports_skip_count(tmp_path: Path) -> None:
    """
    Sibling fixtures exist but none match: The check reports the skip
    count instead of pretending no siblings were generated.
    """
    payload = _payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")
    other_sibling_id = SIBLING_ID.replace("test_a[", "test_b[")
    _write_fixture(tmp_path, SIBLING_FIXTURES_DIR, other_sibling_id, [payload])
    _write_fixture(tmp_path, ENGINE_X_FIXTURES_DIR, ENGINE_X_ID, [payload])

    result = verify_engine_x_execution(tmp_path)

    assert result is not None
    assert result.compared == 0
    assert result.skipped == 1


def _sync_payload() -> Dict[str, Any]:
    """Build a framework sync block's newPayload entry."""
    payload = _payload(gas_used="0x0", state_root="0x33", block_hash="0x44")
    payload["params"][0]["transactions"] = []
    payload["phase"] = "sync"
    return payload


def _shifted(
    payload: Dict[str, Any], *, number: str, timestamp: str
) -> Dict[str, Any]:
    """Return a copy of `payload` at a shifted chain position."""
    shifted = json.loads(json.dumps(payload))
    shifted["params"][0]["blockNumber"] = number
    shifted["params"][0]["timestamp"] = timestamp
    return shifted


def test_sync_payload_is_ignored(tmp_path: Path) -> None:
    """
    An Engine X fixture prepended with a sync-phase payload compares
    clean against its unprepended sibling: the extra payload is
    dropped and the block numbers and timestamps it shifts are
    scrubbed.
    """
    base = _payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")
    _write_fixture(
        tmp_path,
        SIBLING_FIXTURES_DIR,
        SIBLING_ID,
        [_shifted(base, number="0x1", timestamp="0xc")],
    )
    _write_fixture(
        tmp_path,
        ENGINE_X_FIXTURES_DIR,
        ENGINE_X_ID,
        [_sync_payload(), _shifted(base, number="0x2", timestamp="0xd")],
    )

    result = verify_engine_x_execution(tmp_path)

    assert result is not None
    assert result.compared == 1


def test_sync_payload_does_not_mask_drift(tmp_path: Path) -> None:
    """A real execution difference still fails on a prepended fixture."""
    _write_fixture(
        tmp_path,
        SIBLING_FIXTURES_DIR,
        SIBLING_ID,
        [_payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")],
    )
    _write_fixture(
        tmp_path,
        ENGINE_X_FIXTURES_DIR,
        ENGINE_X_ID,
        [
            _sync_payload(),
            _payload(gas_used="0xbeef", state_root="0xaa", block_hash="0xbb"),
        ],
    )

    with pytest.raises(EngineXExecutionDriftError) as exc_info:
        verify_engine_x_execution(tmp_path)

    assert "gasUsed" in str(exc_info.value)


def test_appended_sync_payload_never_enters_the_comparison(
    tmp_path: Path,
) -> None:
    """
    An append-class fixture (out-of-chain ``syncPayload``) compares
    its payload list against the sibling untouched: the appended block
    shifts nothing, so nothing is dropped or scrubbed.
    """
    base = _payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")
    _write_fixture(
        tmp_path,
        SIBLING_FIXTURES_DIR,
        SIBLING_ID,
        [_shifted(base, number="0x1", timestamp="0xc")],
    )
    _write_fixture(
        tmp_path,
        ENGINE_X_FIXTURES_DIR,
        ENGINE_X_ID,
        [_shifted(base, number="0x1", timestamp="0xc")],
        sync_payload=_sync_payload(),
    )

    result = verify_engine_x_execution(tmp_path)

    assert result is not None
    assert result.compared == 1


def test_appended_sync_payload_keeps_full_strictness(tmp_path: Path) -> None:
    """
    An append-class fixture must not inherit the prepend class's
    position scrubbing: a shifted block number is a real difference.
    """
    base = _payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")
    _write_fixture(
        tmp_path,
        SIBLING_FIXTURES_DIR,
        SIBLING_ID,
        [_shifted(base, number="0x1", timestamp="0xc")],
    )
    _write_fixture(
        tmp_path,
        ENGINE_X_FIXTURES_DIR,
        ENGINE_X_ID,
        [_shifted(base, number="0x2", timestamp="0xc")],
        sync_payload=_sync_payload(),
    )

    with pytest.raises(EngineXExecutionDriftError) as exc_info:
        verify_engine_x_execution(tmp_path)

    assert "blockNumber" in str(exc_info.value)


def test_prepended_fixture_scrubs_position_derived_content(
    tmp_path: Path,
) -> None:
    """
    A prepend-class fixture's shifted slot numbers and block access
    list (whose EIP-2935 system write embeds the parent's hash) are
    scrubbed alongside the block number and timestamp; a real
    execution difference (gas used) still fails.
    """
    base = _payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")
    sibling = _shifted(base, number="0x1", timestamp="0xc")
    sibling["params"][0]["slotNumber"] = "0x1"
    sibling["params"][0]["blockAccessList"] = "0xaa"
    engine_x = _shifted(base, number="0x2", timestamp="0xd")
    engine_x["params"][0]["slotNumber"] = "0x2"
    engine_x["params"][0]["blockAccessList"] = "0xbb"
    _write_fixture(tmp_path, SIBLING_FIXTURES_DIR, SIBLING_ID, [sibling])
    _write_fixture(
        tmp_path,
        ENGINE_X_FIXTURES_DIR,
        ENGINE_X_ID,
        [_sync_payload(), engine_x],
    )

    result = verify_engine_x_execution(tmp_path)

    assert result is not None
    assert result.compared == 1


def test_appended_fixture_keeps_access_list_strictness(
    tmp_path: Path,
) -> None:
    """
    The append class must not inherit the prepend class's access-list
    scrub: nothing shifts under an appended sync payload, so a block
    access list difference is a real execution difference.
    """
    base = _payload(gas_used="0x5208", state_root="0x01", block_hash="0x02")
    sibling = json.loads(json.dumps(base))
    sibling["params"][0]["blockAccessList"] = "0xaa"
    engine_x = json.loads(json.dumps(base))
    engine_x["params"][0]["blockAccessList"] = "0xbb"
    _write_fixture(tmp_path, SIBLING_FIXTURES_DIR, SIBLING_ID, [sibling])
    _write_fixture(
        tmp_path,
        ENGINE_X_FIXTURES_DIR,
        ENGINE_X_ID,
        [engine_x],
        sync_payload=_sync_payload(),
    )

    with pytest.raises(EngineXExecutionDriftError) as exc_info:
        verify_engine_x_execution(tmp_path)

    assert "blockAccessList" in str(exc_info.value)
