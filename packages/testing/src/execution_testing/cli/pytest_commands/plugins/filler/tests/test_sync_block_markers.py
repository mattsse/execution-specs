"""
Test the eligibility markers' interaction with the sync block.

A prepend-class marker names a reason the prepended sync block cannot
preserve what an invalid singleton verifies: the extra block shifts
every position, executes the test's own setup, or cannot reproduce a
pinned fee value. A marked test is not skipped - it fills without the
extra block, so no test ever leaves the fixture release; sync-based
consumers skip its single-block chain at consume time instead. The
markers are irrelevant to valid chains: the appended sync block
changes nothing below itself, so a marked valid test keeps its
trailer.
"""

import json
import textwrap
from pathlib import Path
from typing import Any, Dict

import pytest

marked_test_module = textwrap.dedent(
    """\
    import pytest

    from execution_testing import Block, Transaction, TransactionException


    @pytest.mark.{marker}
    @pytest.mark.exception_test
    def test_marked_invalid(blockchain_test, pre) -> None:
        tx = Transaction(
            to=0,
            gas_limit=20_999,
            sender=pre.fund_eoa(),
            error=TransactionException.INTRINSIC_GAS_TOO_LOW,
        )
        blockchain_test(
            pre=pre,
            post={{}},
            blocks=[
                Block(
                    txs=[tx],
                    exception=TransactionException.INTRINSIC_GAS_TOO_LOW,
                )
            ],
        )


    @pytest.mark.exception_test
    def test_unmarked_invalid(blockchain_test, pre) -> None:
        tx = Transaction(
            to=0,
            gas_limit=20_999,
            sender=pre.fund_eoa(),
            error=TransactionException.INTRINSIC_GAS_TOO_LOW,
        )
        blockchain_test(
            pre=pre,
            post={{}},
            blocks=[
                Block(
                    txs=[tx],
                    exception=TransactionException.INTRINSIC_GAS_TOO_LOW,
                )
            ],
        )


    @pytest.mark.{marker}
    def test_marked_valid(blockchain_test, pre) -> None:
        tx = Transaction(
            to=0,
            gas_limit=21_000,
            sender=pre.fund_eoa(),
        )
        blockchain_test(pre=pre, post={{}}, blocks=[Block(txs=[tx])])
    """
)


def make_test_module(pytester: Any, marker: str) -> Any:
    """Write a test module into a pytester tests tree."""
    tests_dir = pytester.mkdir("tests")
    cancun_tests_dir = tests_dir / "cancun"
    cancun_tests_dir.mkdir()
    module_dir = cancun_tests_dir / "sync_block_markers_module"
    module_dir.mkdir()
    test_module = module_dir / "test_marked.py"
    test_module.write_text(marked_test_module.format(marker=marker))
    pytester.copy_example(
        name="src/execution_testing/cli/pytest_commands/pytest_ini_files/pytest-fill.ini"
    )
    return test_module


def fill(pytester: Any, test_module: Any) -> Path:
    """Fill the module in both phases and return the output directory."""
    output = pytester.path / "fixtures"
    common = (
        "-c",
        "pytest-fill.ini",
        "--fork",
        "Cancun",
        "--generate-all-formats",
        "--sync-block",
        "--skip-index",
        "--no-html",
        f"--output={output}",
        str(test_module.relative_to(pytester.path)),
    )
    result = pytester.runpytest_subprocess(
        "--generate-pre-alloc-groups", *common
    )
    assert result.ret == 0, "fill phase 1 was expected to succeed"
    result = pytester.runpytest_subprocess("--use-pre-alloc-groups", *common)
    assert result.ret == 0, "fill phase 2 was expected to succeed"
    return output


def engine_x_fixtures(output: Path) -> Dict[str, Dict[str, Any]]:
    """Return the emitted engine_x fixtures keyed by test id."""
    fixtures: Dict[str, Dict[str, Any]] = {}
    for path in sorted((output / "blockchain_tests_engine_x").rglob("*.json")):
        if "pre_alloc" in path.parts:
            continue
        fixtures.update(json.loads(path.read_text()))
    assert fixtures, "no engine_x fixtures were emitted"
    return fixtures


@pytest.mark.parametrize(
    "marker",
    [
        "absolute_block_position",
        "no_empty_block_fee_preimage",
        "pre_state_affects_empty_block",
    ],
)
def test_marked_test_fills_without_the_sync_block(
    pytester: Any, marker: str
) -> None:
    """
    Each marker must veto the prepend for its own test only: the
    marked invalid singleton fills with exactly its own single block,
    the unmarked invalid singleton in the same module gains the
    prepended one, and the marked valid test keeps its appended
    trailer - the marker means nothing to a chain the extra block
    cannot shift.
    """
    test_module = make_test_module(pytester, marker=marker)
    output = fill(pytester, test_module)

    fixtures = engine_x_fixtures(output)
    by_name = {}
    for test_id, fixture in fixtures.items():
        for name in ("marked_invalid", "unmarked_invalid", "marked_valid"):
            if f"test_{name}[" in test_id:
                by_name[name] = fixture
    assert set(by_name) == {
        "marked_invalid",
        "unmarked_invalid",
        "marked_valid",
    }

    marked_payloads = by_name["marked_invalid"]["engineNewPayloads"]
    assert len(marked_payloads) == 1, (
        "the marked invalid singleton must fill without the sync block"
    )
    assert marked_payloads[0].get("phase") is None
    assert "syncPayload" not in by_name["marked_invalid"]

    unmarked_payloads = by_name["unmarked_invalid"]["engineNewPayloads"]
    assert len(unmarked_payloads) == 2, (
        "the unmarked invalid singleton must still gain the prepended block"
    )
    assert unmarked_payloads[0].get("phase") == "sync"

    valid_payloads = by_name["marked_valid"]["engineNewPayloads"]
    assert len(valid_payloads) == 1
    assert "syncPayload" in by_name["marked_valid"], (
        "a prepend-class marker must not cost a valid chain its trailer"
    )
