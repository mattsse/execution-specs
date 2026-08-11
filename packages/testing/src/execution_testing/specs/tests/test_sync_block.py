"""
Tests for the per-class sync-block policy and its chain transforms.

The filler adds one framework-built empty block to engine_x chains so
sync-based consumers can trigger a devp2p sync; where the block lands
is resolved per test from the chain's statically declared structure:
appended above a fully valid chain (out-of-chain, the fixture's
``sync_payload``), prepended below a single expected-invalid or Engine
API-refused block (in-chain, shifting the singleton up), and nowhere
otherwise. These tests pin the resolution and the prepend transform;
the appended block is built by ``make_hive_fixture`` against a real
backend and is covered by the filler plugin's pytester tests.
"""

from hashlib import sha256
from typing import List

import pytest

from execution_testing.base_types import HexNumber
from execution_testing.exceptions import BlockException, EngineAPIError
from execution_testing.forks import Cancun, Fork, London, Osaka, Shanghai
from execution_testing.specs.benchmark import BenchmarkTest
from execution_testing.specs.blockchain import (
    Block,
    BlockchainTest,
    SyncBlockPolicy,
    empty_block_base_fee_preimage,
    empty_block_excess_blob_gas_preimage,
)
from execution_testing.specs.state import StateTest
from execution_testing.test_types import Alloc, Environment, Transaction

SALT = "tests/cancun/test_x.py::test_y[fork_Cancun-blockchain_test]"

INVALID = Block(
    timestamp=1_000, exception=BlockException.INCORRECT_BLOCK_FORMAT
)
REFUSED = Block(
    timestamp=1_000, engine_api_error_code=EngineAPIError.InvalidParams
)
VALID = Block(timestamp=1_000)


def make_test(
    *, blocks: List[Block], salt: str = SALT, sync_block: bool = True
) -> BlockchainTest:
    """Create a Cancun blockchain test over the given blocks."""
    return BlockchainTest(
        fork=Cancun,
        pre=Alloc(),
        post=Alloc(),
        blocks=blocks,
        sync_block=sync_block,
        sync_block_salt=salt,
    )


@pytest.mark.parametrize(
    "blocks,policy",
    [
        pytest.param([VALID], SyncBlockPolicy.APPEND, id="valid_singleton"),
        pytest.param(
            [VALID, Block(timestamp=2_000)],
            SyncBlockPolicy.APPEND,
            id="valid_multi_block",
        ),
        pytest.param(
            [INVALID], SyncBlockPolicy.PREPEND, id="invalid_singleton"
        ),
        pytest.param(
            [REFUSED],
            SyncBlockPolicy.PREPEND,
            id="engine_refused_singleton",
        ),
        pytest.param(
            [
                Block(
                    timestamp=1_000,
                    exception=BlockException.INCORRECT_BLOCK_FORMAT,
                    engine_api_error_code=EngineAPIError.InvalidParams,
                )
            ],
            SyncBlockPolicy.PREPEND,
            id="invalid_and_refused_singleton",
        ),
        pytest.param(
            [
                VALID,
                Block(
                    timestamp=2_000,
                    exception=BlockException.INCORRECT_BLOCK_FORMAT,
                ),
            ],
            SyncBlockPolicy.NONE,
            id="invalid_multi_block",
        ),
        pytest.param(
            [
                Block(
                    timestamp=1_000,
                    exception=BlockException.INCORRECT_BLOCK_FORMAT,
                ),
                Block(timestamp=2_000),
            ],
            SyncBlockPolicy.NONE,
            id="mid_chain_invalid_block",
        ),
        pytest.param(
            [
                VALID,
                Block(
                    timestamp=2_000,
                    engine_api_error_code=EngineAPIError.InvalidParams,
                ),
            ],
            SyncBlockPolicy.NONE,
            id="engine_refused_multi_block_head",
        ),
        pytest.param([], SyncBlockPolicy.NONE, id="empty_chain"),
    ],
)
def test_sync_block_policy_resolution(
    blocks: List[Block], policy: SyncBlockPolicy
) -> None:
    """
    The policy must follow the chain's statically declared structure.

    An Engine API-refused block must never sit below an appended sync
    block: the appended block would become the announced head, the
    refused block would arrive over devp2p instead of through
    `newPayload`, and the refusal the test asserts would never happen.
    """
    assert make_test(blocks=blocks).sync_block_policy() is policy


def test_sync_block_disabled_resolves_to_none() -> None:
    """Without the option every chain keeps its own structure."""
    test = make_test(blocks=[VALID], sync_block=False)
    assert test.sync_block_policy() is SyncBlockPolicy.NONE
    assert test.blocks_to_build() is test.blocks


def test_append_class_chain_is_untouched() -> None:
    """
    The appended sync block must not appear in the chain's block list:
    it is built on the test's post-state after the chain (and its
    post-state verification) is done, and lives out-of-chain in the
    fixture's ``sync_payload`` field.
    """
    test = make_test(blocks=[VALID])
    assert test.blocks_to_build() is test.blocks


def test_prepend_inserts_one_salted_empty_block() -> None:
    """
    The prepend-class chain gains one empty block at genesis + 1
    carrying a digest of the test's salt in its ``extra_data``.
    """
    test = make_test(blocks=[INVALID])
    blocks = test.blocks_to_build()
    assert len(blocks) == 2
    prepended = blocks[0]
    assert prepended.timestamp == 1
    assert prepended.txs == []
    assert prepended.exception is None
    assert prepended.extra_data == sha256(SALT.encode()).digest()[:16]
    assert blocks[1] is test.blocks[0], "the test's own block passes through"


def test_sync_block_differs_per_test() -> None:
    """
    Tests of a pre-allocation group must not share the sync block: a
    client that already knows the announced head never starts a sync.
    """
    first = make_test(blocks=[INVALID], salt="a").blocks_to_build()
    second = make_test(blocks=[INVALID], salt="b").blocks_to_build()
    assert first[0].extra_data != second[0].extra_data


def test_prepend_shifts_a_pinned_block_number() -> None:
    """A block pinning an absolute number shifts up to stay contiguous."""
    test = make_test(
        blocks=[
            Block(
                number=1,
                timestamp=1_000,
                exception=BlockException.INCORRECT_BLOCK_FORMAT,
            )
        ]
    )
    numbers = [block.number for block in test.blocks_to_build()]
    assert numbers == [None, 2]
    assert test.blocks[0].number == 1, "the test's own block is untouched"


def test_expected_invalid_block_is_exempt_from_the_timestamp_walk() -> None:
    """
    An expected-invalid block pinning the prepended block's own
    timestamp still fills: it is rejected and rolled back anyway, and
    the collision only adds one more reason.
    """
    test = make_test(
        blocks=[
            Block(timestamp=1, exception=BlockException.INCORRECT_BLOCK_FORMAT)
        ]
    )
    assert len(test.blocks_to_build()) == 2


def test_engine_refused_block_timestamp_collision_is_refused() -> None:
    """
    An Engine API-refused block is consensus-valid, so a pinned
    timestamp the prepended block does not clear would build a
    non-monotonic - consensus-invalid - chain and must fail loudly.
    """
    test = make_test(
        blocks=[
            Block(
                timestamp=1,
                engine_api_error_code=EngineAPIError.InvalidParams,
            )
        ]
    )
    with pytest.raises(ValueError, match="does not clear its parent"):
        test.blocks_to_build()


def test_benchmark_tests_opt_out_of_the_sync_block() -> None:
    """
    Benchmark tests must never receive a sync block: a framework block
    in the chain would distort their per-block measurements.
    """
    assert BenchmarkTest.supports_sync_block is False
    assert BlockchainTest.supports_sync_block is True
    assert StateTest.supports_sync_block is True


def test_sync_block_timestamp_headroom() -> None:
    """
    A head timestamp must leave the appended sync block room below
    the uint64 ceiling: the last parent the increment clears passes,
    and the first that overflows - and the ceiling itself - are
    refused loudly.
    """
    test = make_test(blocks=[VALID])
    test._verify_sync_block_timestamp_headroom(2**64 - 13)
    with pytest.raises(ValueError, match="no uint64 headroom"):
        test._verify_sync_block_timestamp_headroom(2**64 - 12)
    with pytest.raises(ValueError, match="no uint64 headroom"):
        test._verify_sync_block_timestamp_headroom(2**64 - 1)


def test_state_test_conversion_carries_the_sync_block_fields() -> None:
    """
    A converted state test resolves its policy like the blockchain
    test it becomes: its single valid block appends, and the salt
    rides along.
    """
    state_test = StateTest(
        fork=Cancun,
        pre=Alloc(),
        post=Alloc(),
        tx=Transaction(sender=HexNumber(0), to=HexNumber(0), nonce=0),
        env=Environment(number=1, timestamp=1_000),
        sync_block=True,
        sync_block_salt=SALT,
    )
    blockchain_test = state_test.generate_blockchain_test()
    assert blockchain_test.sync_block_policy() is SyncBlockPolicy.APPEND
    assert blockchain_test.sync_block_salt == SALT


GAS_LIMIT = 100_000_000
CANCUN_TARGET_BLOB_GAS = (
    Cancun.target_blobs_per_block() * Cancun.blob_gas_per_blob()
)


@pytest.mark.parametrize(
    "base_fee_per_gas",
    [0, 1, 6, 7, 8, 9, 10, 100, 1_000, 875_000_000, 1_000_000_000],
)
@pytest.mark.parametrize("fork", [London, Cancun])
def test_base_fee_preimage_round_trip(
    fork: Fork, base_fee_per_gas: int
) -> None:
    """An empty block must decay the preimage to the intended value."""
    preimage = empty_block_base_fee_preimage(
        fork=fork,
        base_fee_per_gas=base_fee_per_gas,
        gas_limit=GAS_LIMIT,
    )
    derived = fork.base_fee_per_gas_calculator()(
        parent_base_fee_per_gas=preimage,
        parent_gas_used=0,
        parent_gas_limit=GAS_LIMIT,
    )
    assert derived == base_fee_per_gas


@pytest.mark.parametrize(
    "excess_blob_gas",
    [
        0,
        0x20000,
        0x60000,
        0x80000,
        0xE0000,
        0x12345,  # not a multiple of the blob gas quantum
    ],
)
def test_excess_blob_gas_preimage_round_trip(excess_blob_gas: int) -> None:
    """An empty block must decay the preimage to the intended value."""
    preimage = empty_block_excess_blob_gas_preimage(
        fork=Cancun,
        excess_blob_gas=excess_blob_gas,
        parent_base_fee_per_gas=7,
    )
    derived = Cancun.excess_blob_gas_calculator()(
        parent_excess_blob_gas=preimage,
        parent_blob_gas_used=0,
        parent_base_fee_per_gas=7,
    )
    assert derived == excess_blob_gas


@pytest.mark.parametrize("excess_blobs", [1, 2, 3, 4, 5])
def test_excess_blob_gas_with_no_preimage_is_refused(
    excess_blobs: int,
) -> None:
    """
    A value the reserve price makes unreachable must be refused.

    Above the reserve price an empty block leaves the excess where it
    is, so a genesis winding up to a small nonzero excess does not
    exist. Refusing loudly here is what keeps a silently shifted fee
    environment out of the fixtures.
    """
    with pytest.raises(ValueError, match="no parent excess blob gas"):
        empty_block_excess_blob_gas_preimage(
            fork=Osaka,
            excess_blob_gas=excess_blobs * Osaka.blob_gas_per_blob(),
            parent_base_fee_per_gas=17,
        )


def make_fee_pinning_test(*, blocks: List[Block]) -> BlockchainTest:
    """Create a Cancun blockchain test pinning genesis fee values."""
    return BlockchainTest(
        fork=Cancun,
        pre=Alloc(),
        post=Alloc(),
        blocks=blocks,
        genesis_environment=Environment(
            base_fee_per_gas=HexNumber(10),
            excess_blob_gas=HexNumber(0x80000),
        ),
        sync_block=True,
        sync_block_salt="test",
    )


def test_append_class_genesis_is_not_compensated() -> None:
    """
    A valid chain's genesis passes through untouched: nothing sits
    below the test's own blocks, so there is no fee step to cancel.
    """
    env = make_fee_pinning_test(blocks=[VALID]).get_genesis_environment()
    assert env.base_fee_per_gas == 10
    assert env.excess_blob_gas == 0x80000


def test_prepend_class_genesis_compensation() -> None:
    """
    One empty-block step from an invalid singleton's compensated
    genesis lands on the author's genesis values.
    """
    env = make_fee_pinning_test(blocks=[INVALID]).get_genesis_environment()
    assert env.base_fee_per_gas is not None
    assert env.excess_blob_gas is not None
    derived_base_fee = Cancun.base_fee_per_gas_calculator()(
        parent_base_fee_per_gas=int(env.base_fee_per_gas),
        parent_gas_used=0,
        parent_gas_limit=int(env.gas_limit),
    )
    assert derived_base_fee == 10
    derived_excess = Cancun.excess_blob_gas_calculator()(
        parent_excess_blob_gas=int(env.excess_blob_gas),
        parent_blob_gas_used=0,
        parent_base_fee_per_gas=int(env.base_fee_per_gas),
    )
    assert derived_excess == 0x80000


def test_prepend_class_pre_blob_genesis_compensation() -> None:
    """A pre-Cancun invalid singleton compensates the base fee only."""
    test = BlockchainTest(
        fork=Shanghai,
        pre=Alloc(),
        post=Alloc(),
        blocks=[INVALID],
        genesis_environment=Environment(base_fee_per_gas=HexNumber(1000)),
        sync_block=True,
        sync_block_salt="test",
    )
    env = test.get_genesis_environment()
    assert env.base_fee_per_gas is not None
    assert env.excess_blob_gas is None
    derived_base_fee = Shanghai.base_fee_per_gas_calculator()(
        parent_base_fee_per_gas=int(env.base_fee_per_gas),
        parent_gas_used=0,
        parent_gas_limit=int(env.gas_limit),
    )
    assert derived_base_fee == 1000


def make_invalid_state_test(*, sync_block: bool) -> StateTest:
    """
    Create a Cancun state test that converts to an invalid singleton
    while pinning its block fee values.
    """
    return StateTest(
        fork=Cancun,
        pre=Alloc(),
        post=Alloc(),
        tx=Transaction(sender=HexNumber(0), to=HexNumber(0), nonce=0),
        env=Environment(
            number=1,
            timestamp=1_000,
            base_fee_per_gas=HexNumber(10),
            excess_blob_gas=HexNumber(0xE0000),
        ),
        block_exception=BlockException.INCORRECT_BLOCK_FORMAT,
        sync_block=sync_block,
        sync_block_salt="test",
    )


def test_invalid_state_test_composition() -> None:
    """
    Two empty-block steps from a converted invalid state test's
    compensated genesis land on the pinned block environment.

    The state-test conversion winds genesis one step up from the
    pinned block environment; the prepend compensation adds the one
    further step the prepended sync block consumes.
    """
    blockchain_test = make_invalid_state_test(
        sync_block=True
    ).generate_blockchain_test()
    assert blockchain_test.sync_block_policy() is SyncBlockPolicy.PREPEND
    env = blockchain_test.get_genesis_environment()
    assert env.base_fee_per_gas is not None
    assert env.excess_blob_gas is not None

    base_fee = int(env.base_fee_per_gas)
    excess = int(env.excess_blob_gas)
    base_fee_calculator = Cancun.base_fee_per_gas_calculator()
    excess_calculator = Cancun.excess_blob_gas_calculator()
    for _ in range(2):
        new_excess = excess_calculator(
            parent_excess_blob_gas=excess,
            parent_blob_gas_used=0,
            parent_base_fee_per_gas=base_fee,
        )
        base_fee = base_fee_calculator(
            parent_base_fee_per_gas=base_fee,
            parent_gas_used=0,
            parent_gas_limit=int(env.gas_limit),
        )
        excess = new_excess
    assert base_fee == 10
    assert excess == 0xE0000


def test_state_test_conversion_unchanged_without_sync_block() -> None:
    """Without the option the conversion winds exactly one step up."""
    blockchain_test = make_invalid_state_test(
        sync_block=False
    ).generate_blockchain_test()
    env = blockchain_test.get_genesis_environment()
    assert env.base_fee_per_gas == 10 * 8 // 7
    assert env.excess_blob_gas == 0xE0000 + CANCUN_TARGET_BLOB_GAS


def test_prepend_ineligibility_marker_falls_back_bare() -> None:
    """
    A marked invalid singleton fills with exactly its own chain: the
    marker vetoes the prepend, and the test is never skipped.
    """
    test = BlockchainTest(
        fork=Cancun,
        pre=Alloc(),
        post=Alloc(),
        blocks=[INVALID],
        sync_block=True,
        sync_block_salt="test",
        sync_block_ineligibilities=frozenset({"absolute_block_position"}),
    )
    assert test.sync_block_policy() is SyncBlockPolicy.NONE
    assert test.blocks_to_build() is test.blocks


def test_prepend_ineligibility_marker_is_irrelevant_to_append() -> None:
    """
    A prepend-class marker on a valid chain changes nothing: the
    appended sync block shifts no position, so the chain keeps its
    trailer.
    """
    test = BlockchainTest(
        fork=Cancun,
        pre=Alloc(),
        post=Alloc(),
        blocks=[VALID],
        sync_block=True,
        sync_block_salt="test",
        sync_block_ineligibilities=frozenset({"absolute_block_position"}),
    )
    assert test.sync_block_policy() is SyncBlockPolicy.APPEND


def test_prepend_ineligibility_restores_the_author_genesis() -> None:
    """
    The marker's veto must reach the genesis compensation: a marked
    invalid singleton's pinned fee environment passes through
    untouched, in both fill phases.
    """
    test = BlockchainTest(
        fork=Cancun,
        pre=Alloc(),
        post=Alloc(),
        blocks=[INVALID],
        genesis_environment=Environment(
            base_fee_per_gas=HexNumber(10),
            excess_blob_gas=HexNumber(0x80000),
        ),
        sync_block=True,
        sync_block_salt="test",
        sync_block_ineligibilities=frozenset({"absolute_block_position"}),
    )
    env = test.get_genesis_environment()
    assert env.base_fee_per_gas == 10
    assert env.excess_blob_gas == 0x80000
