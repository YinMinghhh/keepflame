"""Composition root for concrete KeepFlame runtime resources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from keepflame.capabilities import CapabilityRegistry, FileSecretResolver, UrllibHttpClient
from keepflame.capabilities.browser import IsolatedBrowser
from keepflame.core.config import AppConfig, load_config
from keepflame.core.orchestrator import Orchestrator, SystemClock
from keepflame.core.plugins import EntryPointKeeperRegistry
from keepflame.core.protocols import Notifier
from keepflame.core.service import SchedulerService
from keepflame.notifier import JsonlNotifier
from keepflame.storage import SQLiteStorage


@dataclass(slots=True)
class Runtime:
    config: AppConfig
    storage: SQLiteStorage
    notifier: JsonlNotifier
    orchestrator: Orchestrator
    service: SchedulerService


def build_runtime(
    config_path: str | Path | None = None, *, stream: TextIO | None = None,
    delivery_notifier: Notifier | None = None,
) -> Runtime:
    config = load_config(config_path)
    storage = SQLiteStorage(config.storage.path)
    notifier = JsonlNotifier(stream)
    clock = SystemClock()
    registry = EntryPointKeeperRegistry()
    for keeper_name in {flame.keeper for flame in config.enabled_flames()}:
        registry.resolve(keeper_name)
    orchestrator = Orchestrator(
        storage=storage,
        registry=registry,
        capabilities=CapabilityRegistry({"http": UrllibHttpClient(), "browser": IsolatedBrowser()}),
        secrets=FileSecretResolver(),
        notifier=delivery_notifier if delivery_notifier is not None else notifier,
        clock=clock,
    )
    service = SchedulerService(
        config=config,
        storage=storage,
        orchestrator=orchestrator,
        clock=clock,
    )
    return Runtime(
        config=config,
        storage=storage,
        notifier=notifier,
        orchestrator=orchestrator,
        service=service,
    )
