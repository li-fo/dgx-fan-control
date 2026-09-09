from __future__ import annotations

import asyncio
import sqlite3
import time
import zlib
from pathlib import Path

import pytest

from dgx_fan.config import EndpointConfig
from dgx_fan.history import HistoryService

DCGM = """DCGM_FI_DEV_GPU_TEMP{UUID="GPU-a"} 50
DCGM_FI_DEV_GPU_UTIL{UUID="GPU-a"} 40
DCGM_FI_DEV_FB_USED{UUID="GPU-a"} 20
DCGM_FI_DEV_FB_FREE{UUID="GPU-a"} 60
DCGM_FI_DEV_FB_RESERVED{UUID="GPU-a"} 20
DCGM_FI_DEV_POWER_USAGE{UUID="GPU-a"} 100
DCGM_FI_DEV_GPU_TEMP{UUID="GPU-mig",GPU_I_ID="1"} 99
DCGM_FI_DEV_POWER_USAGE{UUID="GPU-mig",GPU_I_ID="1"} 999
"""
NODE = "node_memory_MemTotal_bytes 1000\nnode_memory_MemAvailable_bytes 700\nnode_cpu_seconds_total 9\n"


def _service(tmp_path: Path, *, memory_source: str = "dcgm") -> HistoryService:
    endpoint = EndpointConfig("one", "One", "http://one", memory_source, "http://node" if memory_source == "node-exporter" else None)
    return HistoryService(tmp_path / "config.toml", (endpoint,))


@pytest.mark.asyncio
async def test_archives_raw_dcgm_including_mig_and_queries_derived_values(tmp_path: Path) -> None:
    service = _service(tmp_path)
    now = time.time()
    await service.start()
    service.submit("one", "dcgm", DCGM, now, 2.0)
    await asyncio.sleep(0.08)
    result = await service.query("one", now - 1, now + 1, 2)
    await service.close()
    assert result["series"] == {"memory": [None, 20.0], "utilization": [None, 40.0], "temperature": [None, 50.0], "power": [None, 100.0]}
    connection = sqlite3.connect(service.db_path)
    blob = connection.execute("SELECT raw_zlib FROM history_scrapes").fetchone()[0]
    connection.close()
    archived = zlib.decompress(blob).decode()
    assert 'GPU_I_ID="1"' in archived
    assert "node_cpu_seconds_total" not in archived


@pytest.mark.asyncio
async def test_node_memory_is_selected_and_restart_persists(tmp_path: Path) -> None:
    now = time.time()
    service = _service(tmp_path, memory_source="node-exporter")
    await service.start()
    service.submit("one", "dcgm", DCGM, now, 2.0)
    service.submit("one", "node", NODE, now, 2.0)
    await service.close()
    reopened = _service(tmp_path, memory_source="node-exporter")
    result = await reopened.query("one", now - 1, now + 1, 1)
    assert result["series"] == {"memory": [30.0], "utilization": [40.0], "temperature": [50.0], "power": [100.0]}


@pytest.mark.asyncio
async def test_failures_and_missing_power_are_gaps(tmp_path: Path) -> None:
    service = _service(tmp_path)
    now = time.time()
    no_power = DCGM.replace('DCGM_FI_DEV_POWER_USAGE{UUID="GPU-a"} 100\n', "")
    await service.start()
    service.submit("one", "dcgm", no_power, now, 2.0)
    service.submit("one", "dcgm", None, now + 1, 2.0, error="offline")
    await asyncio.sleep(0.08)
    result = await service.query("one", now - 1, now + 2, 1)
    await service.close()
    assert result["series"]["power"] == [None]
    connection = sqlite3.connect(service.db_path)
    assert connection.execute("SELECT error FROM history_scrapes WHERE error IS NOT NULL").fetchone()[0] == "offline"
    connection.close()


@pytest.mark.asyncio
async def test_second_physical_gpu_without_power_makes_total_power_unavailable(tmp_path: Path) -> None:
    service = _service(tmp_path)
    now = time.time()
    two_gpu = DCGM + 'DCGM_FI_DEV_GPU_TEMP{UUID="GPU-b"} 60\nDCGM_FI_DEV_GPU_UTIL{UUID="GPU-b"} 80\n'
    await service.start()
    service.submit("one", "dcgm", two_gpu, now, 2.0)
    await service.close()
    result = await service.query("one", now - 1, now + 1, 1)
    assert result["series"] == {"memory": [None], "utilization": [80.0], "temperature": [60.0], "power": [None]}


@pytest.mark.asyncio
async def test_retention_and_invalid_requests(tmp_path: Path) -> None:
    service = _service(tmp_path)
    now = time.time()
    await service.start()
    service.submit("one", "dcgm", DCGM, now - 8 * 24 * 60 * 60 - 1, 2.0)
    await asyncio.sleep(0.08)
    await service.close()
    connection = sqlite3.connect(service.db_path)
    assert connection.execute("SELECT COUNT(*) FROM history_scrapes").fetchone()[0] == 0
    connection.close()
    with pytest.raises(ValueError):
        await service.query("missing", now - 1, now, 1)
    with pytest.raises(ValueError):
        await service.query("one", now - 9 * 24 * 60 * 60, now, 1)
    with pytest.raises(ValueError):
        await service.query("one", True, now, 1)
    with pytest.raises(ValueError):
        service.submit("one", "other", DCGM, now, 2.0)
    with pytest.raises(ValueError):
        service.submit("one", "dcgm", DCGM, True, 2.0)


@pytest.mark.asyncio
async def test_queue_overflow_is_nonblocking_operator_warning(tmp_path: Path) -> None:
    service = _service(tmp_path)
    # A deliberately stopped writer makes the bounded queue state deterministic.
    service._started = True
    for _ in range(128):
        service.submit("one", "dcgm", DCGM, time.time(), 2.0)
    service.submit("one", "dcgm", DCGM, time.time(), 2.0)
    result = await service.query("one", time.time() - 1, time.time(), 1)
    assert result["status"] == "history response dropped: writer queue is full"
