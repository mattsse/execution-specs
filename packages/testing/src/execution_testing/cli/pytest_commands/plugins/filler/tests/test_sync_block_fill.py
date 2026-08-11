"""
Test that the sync block reaches exactly the fixture formats that opt
in, in the placement each chain class calls for.

The sync block is scoped per fixture format: only
``blockchain_test_engine_x`` declares ``sync_block`` true, so a fill
with ``--sync-block`` must emit engine_x fixtures carrying the extra
block - appended out-of-chain (the ``syncPayload`` field) for a valid
chain, prepended in-chain (a leading payload tagged with the ``sync``
phase) for an invalid singleton - while every other format's chains
are byte-for-byte what the test defines. These tests fill single-block
tests and read the fixtures back, so a sync block that leaks into the
wrong format or placement, loses its phase tag, or fails to salt per
test fails here rather than in a consumer.
"""

import json
import textwrap
from pathlib import Path
from typing import Any, Dict

valid_test_module = textwrap.dedent(
    """\
    import pytest

    from execution_testing import Block, Transaction


    # Two tests of one pre-allocation group: a client reused across
    # them must not already know either announced sync block.
    @pytest.mark.parametrize("value", [1, 2])
    def test_single_block(blockchain_test, pre, value) -> None:
        tx = Transaction(
            to=0,
            value=value,
            gas_limit=21_000,
            sender=pre.fund_eoa(),
        )
        blockchain_test(pre=pre, post={}, blocks=[Block(txs=[tx])])
    """
)

invalid_singleton_module = textwrap.dedent(
    """\
    import pytest

    from execution_testing import Block, Transaction, TransactionException


    @pytest.mark.exception_test
    def test_invalid_singleton(blockchain_test, pre) -> None:
        tx = Transaction(
            to=0,
            gas_limit=20_999,
            sender=pre.fund_eoa(),
            error=TransactionException.INTRINSIC_GAS_TOO_LOW,
        )
        blockchain_test(
            pre=pre,
            post={},
            blocks=[
                Block(
                    txs=[tx],
                    exception=TransactionException.INTRINSIC_GAS_TOO_LOW,
                )
            ],
        )
    """
)


def make_test_module(pytester: Any, source: str, name: str) -> Any:
    """Write a test module into a pytester tests tree."""
    tests_dir = pytester.mkdir("tests")
    cancun_tests_dir = tests_dir / "cancun"
    cancun_tests_dir.mkdir()
    module_dir = cancun_tests_dir / "sync_block_fill_module"
    module_dir.mkdir()
    test_module = module_dir / name
    test_module.write_text(source)
    pytester.copy_example(
        name="src/execution_testing/cli/pytest_commands/pytest_ini_files/pytest-fill.ini"
    )
    return test_module


def fill(pytester: Any, test_module: Any, *args: str) -> Path:
    """
    Fill the module into a fresh output directory and return it.

    An all-formats fill is two pytest sessions - the `fill` CLI runs
    phase 1 (pre-allocation grouping) and phase 2 (fixture filling)
    back to back - so both are run here the same way.
    """
    output = pytester.path / f"fixtures{len(args)}"
    common = (
        "-c",
        "pytest-fill.ini",
        "--fork",
        "Cancun",
        "--generate-all-formats",
        "--skip-index",
        "--no-html",
        f"--output={output}",
        *args,
        str(test_module.relative_to(pytester.path)),
    )
    result = pytester.runpytest_subprocess(
        "--generate-pre-alloc-groups", *common
    )
    assert result.ret == 0, "fill phase 1 was expected to succeed"
    result = pytester.runpytest_subprocess("--use-pre-alloc-groups", *common)
    assert result.ret == 0, "fill phase 2 was expected to succeed"
    return output


# Fixture directory to the format name that appears in a test id.
FORMATS = {
    "blockchain_tests": "blockchain_test",
    "blockchain_tests_engine": "blockchain_test_engine",
    "blockchain_tests_engine_x": "blockchain_test_engine_x",
}


def fixtures_of_format(
    output: Path, format_dir: str
) -> Dict[str, Dict[str, Any]]:
    """
    Return the fixtures emitted in a format's directory, keyed by test
    id with the format's own name stripped out so the same test's
    fixtures line up across formats.
    """
    fixtures: Dict[str, Dict[str, Any]] = {}
    for path in sorted((output / format_dir).rglob("*.json")):
        if "pre_alloc" in path.parts:
            continue
        for test_id, fixture in json.loads(path.read_text()).items():
            key = test_id.replace(f"-{FORMATS[format_dir]}", "")
            fixtures[key] = fixture
    assert fixtures, f"no {format_dir} fixtures were emitted"
    return fixtures


def test_valid_chain_appends_out_of_chain(pytester: Any) -> None:
    """
    A valid chain's sync block is appended: the engine_x payload list
    stays exactly the test's own chain (as does every other format's),
    and the extra block rides out-of-chain in ``syncPayload``, built
    on the test's head.
    """
    test_module = make_test_module(
        pytester, valid_test_module, "test_single_block.py"
    )
    output = fill(pytester, test_module, "--sync-block")

    for fixture in fixtures_of_format(output, "blockchain_tests").values():
        assert len(fixture["blocks"]) == 1
    for fixture in fixtures_of_format(
        output, "blockchain_tests_engine"
    ).values():
        payloads = fixture["engineNewPayloads"]
        assert len(payloads) == 1
        assert payloads[0].get("phase") is None
        assert "syncPayload" not in fixture

    for fixture in fixtures_of_format(
        output, "blockchain_tests_engine_x"
    ).values():
        payloads = fixture["engineNewPayloads"]
        assert len(payloads) == 1, (
            "the appended sync block must not enter the payload list"
        )
        assert payloads[0].get("phase") is None
        own = payloads[0]["params"][0]

        sync_payload = fixture["syncPayload"]
        assert sync_payload["phase"] == "sync", (
            "the sync payload must carry the sync phase tag"
        )
        appended = sync_payload["params"][0]
        assert appended["transactions"] == [], (
            "the sync block carries no transactions"
        )
        assert appended["parentHash"] == own["blockHash"], (
            "the sync block must build on the test's own head"
        )
        assert int(appended["blockNumber"], 16) == 2
        assert int(appended["timestamp"], 16) > int(own["timestamp"], 16)
        assert len(appended["extraData"]) == 2 + 2 * 16, (
            "the sync block is salted with a digest of the test id"
        )
        assert fixture["lastblockhash"] == own["blockHash"], (
            "the fixture's head stays the author's own block"
        )


def test_appended_sync_block_is_salted_per_test(pytester: Any) -> None:
    """
    Each test's sync block must be unique to it, so every announced
    head is a block the group's reused client has never seen and each
    sync attempt is attributable to its own test.
    """
    test_module = make_test_module(
        pytester, valid_test_module, "test_single_block.py"
    )
    output = fill(pytester, test_module, "--sync-block")

    salted = {
        fixture["syncPayload"]["params"][0]["extraData"]
        for fixture in fixtures_of_format(
            output, "blockchain_tests_engine_x"
        ).values()
    }
    assert len(salted) == 2


def test_invalid_singleton_prepends_in_chain(pytester: Any) -> None:
    """
    An invalid singleton's sync block is prepended: it is load-bearing
    ancestry every consumer must replay, so it lives in-chain as the
    first payload, tagged with the sync phase, and the test's own
    block shifts up by one. No format gains an out-of-chain payload.
    """
    test_module = make_test_module(
        pytester, invalid_singleton_module, "test_invalid_singleton.py"
    )
    output = fill(pytester, test_module, "--sync-block")

    for fixture in fixtures_of_format(output, "blockchain_tests").values():
        assert len(fixture["blocks"]) == 1
    for fixture in fixtures_of_format(
        output, "blockchain_tests_engine"
    ).values():
        payloads = fixture["engineNewPayloads"]
        assert len(payloads) == 1
        assert payloads[0].get("phase") is None

    for fixture in fixtures_of_format(
        output, "blockchain_tests_engine_x"
    ).values():
        payloads = fixture["engineNewPayloads"]
        assert len(payloads) == 2
        prepended, own = (payload["params"][0] for payload in payloads)
        assert payloads[0].get("phase") == "sync", (
            "the prepended payload must carry the sync phase tag"
        )
        assert payloads[1].get("phase") is None
        assert prepended["transactions"] == [], (
            "the prepended block carries no transactions"
        )
        assert int(prepended["blockNumber"], 16) == 1
        assert int(own["blockNumber"], 16) == 2
        assert own["parentHash"] == prepended["blockHash"]
        assert "syncPayload" not in fixture, (
            "nothing can be appended above an invalid head"
        )


def test_sync_block_is_off_by_default(pytester: Any) -> None:
    """Without the option no format gains a sync block."""
    test_module = make_test_module(
        pytester, valid_test_module, "test_single_block.py"
    )
    output = fill(pytester, test_module)

    for fixture in fixtures_of_format(output, "blockchain_tests").values():
        assert len(fixture["blocks"]) == 1
    for format_dir in (
        "blockchain_tests_engine",
        "blockchain_tests_engine_x",
    ):
        for fixture in fixtures_of_format(output, format_dir).values():
            payloads = fixture["engineNewPayloads"]
            assert len(payloads) == 1
            assert payloads[0].get("phase") is None
            assert "syncPayload" not in fixture
