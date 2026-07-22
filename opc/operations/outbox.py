"""Background delivery worker for the durable operations outbox."""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger

from opc.core.models import OPCEvent
from opc.operations.durable import DurableRunKernel
from opc.operations.models import OutboxMessage


OutboxHandler = Callable[[OutboxMessage], Awaitable[Any] | Any]


@dataclass(frozen=True)
class OutboxDispatchReport:
    claimed: int = 0
    delivered: int = 0
    failed: int = 0
    dead_lettered: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "claimed": self.claimed,
            "delivered": self.delivered,
            "failed": self.failed,
            "dead_lettered": self.dead_lettered,
        }


class OutboxDispatcher:
    """Continuously deliver claimed messages with lease-token fencing."""

    def __init__(
        self,
        kernel: DurableRunKernel,
        handler: OutboxHandler,
        *,
        worker_id: str = "",
        batch_size: int = 50,
        poll_seconds: float = 1.0,
    ) -> None:
        self.kernel = kernel
        self.handler = handler
        self.worker_id = worker_id or f"openopc-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.batch_size = max(1, min(int(batch_size), 500))
        self.poll_seconds = max(0.05, float(poll_seconds))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def dispatch_once(self) -> OutboxDispatchReport:
        messages = await self.kernel.claim_outbox(
            worker_id=self.worker_id,
            limit=self.batch_size,
        )
        delivered = 0
        failed = 0
        dead_lettered = 0
        for message in messages:
            try:
                result = self.handler(message)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                updated = await self.kernel.fail_outbox(
                    message.message_id,
                    worker_id=self.worker_id,
                    lease_token=message.lease_token,
                    error=f"{type(exc).__name__}: {exc}",
                )
                failed += 1
                dead_lettered += int(updated.status == "dead_letter")
                logger.warning(
                    "Operations outbox delivery failed topic={} message={} status={}: {}",
                    message.topic,
                    message.message_id,
                    updated.status,
                    exc,
                )
                continue
            await self.kernel.acknowledge_outbox(
                message.message_id,
                worker_id=self.worker_id,
                lease_token=message.lease_token,
            )
            delivered += 1
        return OutboxDispatchReport(
            claimed=len(messages),
            delivered=delivered,
            failed=failed,
            dead_lettered=dead_lettered,
        )

    async def start(self) -> None:
        if self.running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(),
            name=f"operations-outbox:{self.worker_id}",
        )

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=max(1.0, self.poll_seconds * 2))
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                report = await self.dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.opt(exception=True).error("Operations outbox dispatcher cycle failed")
                report = OutboxDispatchReport()
            if report.claimed:
                continue
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                continue


def event_bus_handler(event_bus: Any) -> OutboxHandler:
    """Translate an outbox message to the process-wide internal event bus."""

    async def publish(message: OutboxMessage) -> None:
        publisher = getattr(event_bus, "publish_checked", event_bus.publish)
        await publisher(
            OPCEvent(
                event_type=message.topic,
                event_id=message.event_id or message.message_id,
                payload={
                    **dict(message.payload),
                    "outbox_message_id": message.message_id,
                    "run_id": message.run_id,
                },
            )
        )

    return publish
