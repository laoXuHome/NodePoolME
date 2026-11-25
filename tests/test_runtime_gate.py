import asyncio

from backend.scheduler import manager
from backend.services.runtime_gate import gate


def test_gate_allows_only_one_owner():
    async def scenario():
        await gate.release("speed")
        await gate.release("detect")
        assert await gate.try_acquire("speed", "下载测速")
        assert gate.snapshot()["owner"] == "speed"
        assert not await gate.try_acquire("detect", "节点检测评分")
        await gate.release("speed")
        assert gate.snapshot()["owner"] is None
        assert await gate.try_acquire("detect", "节点检测评分")
        await gate.release("detect")

    asyncio.run(scenario())


def test_detection_waits_for_speed_and_then_starts(monkeypatch):
    calls = []

    async def fake_cycle(force=False, group_ids=None, reason="schedule"):
        calls.append(reason)
        return {"ok": True}

    monkeypatch.setattr(manager, "_run_cycle", fake_cycle)

    async def scenario():
        await gate.release("speed")
        await gate.release("detect")
        assert await gate.try_acquire("speed", "下载测速")
        task = asyncio.create_task(manager.run_cycle(force=True, reason="test"))
        await asyncio.sleep(0)
        assert manager.state.deferred is True
        assert manager.state.phase == "等待下载测速完成"
        assert calls == []
        await gate.release("speed")
        result = await asyncio.wait_for(task, timeout=2)
        assert result == {"ok": True}
        assert calls == ["test"]
        assert manager.state.deferred is False
        assert gate.snapshot()["owner"] is None

    asyncio.run(scenario())
