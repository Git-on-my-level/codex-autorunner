from __future__ import annotations

import asyncio

import pytest

from tests.support.waits import wait_for_async_predicate


@pytest.mark.asyncio
async def test_wait_for_async_predicate_times_out_hung_awaitable() -> None:
    async def _never_finishes() -> bool:
        await asyncio.Event().wait()
        return True

    with pytest.raises(AssertionError, match="waiting for hung predicate"):
        await wait_for_async_predicate(
            _never_finishes,
            timeout_seconds=0.01,
            description="hung predicate",
        )
