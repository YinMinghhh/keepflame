"""Composition root for concrete KeepFlame runtime resources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO

from keepflame.capabilities import CapabilityRegistry, FileSecretResolver, UrllibHttpClient
from keepflame.capabilities.browser import IsolatedBrowser, OwnedBrowserSession
from keepflame.capabilities.browser_pool import BrowserPool
from keepflame.login import LoginCoordinator
from keepflame.core.config import AppConfig, ConfigError, load_config
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
    owned_browser: BrowserPool | None = None
    login: LoginCoordinator | None = None
    configuration_blocked: bool = False

    def poll_controls(self):
        for request in self.storage.runtime_commands():
            try:
                now = self.orchestrator.clock.now()
                if now - datetime.fromisoformat(request['created_at']) > timedelta(minutes=15):
                    self.storage.finish_command(request['id'], 'Expired')
                    continue
                updated = load_config(self.config.source_path)
                if updated.storage.path.resolve() != self.storage.path.resolve():
                    raise ConfigError("reload cannot change the runtime database")
                for name in {flame.keeper for flame in updated.enabled_flames()}:
                    self.orchestrator.registry.resolve(name)
                self.config = self.service.config = updated
                self.login.config = updated
                self.configuration_blocked = False
                self.storage.finish_command(request['id'])
                self.notifier.emit('configuration_reloaded', request_id=request['id'])
            except Exception as exc:
                # Preserve the existing context and action ledger but stop task dispatch.
                self.configuration_blocked = True
                self.storage.finish_command(request['id'], type(exc).__name__)
                self.notifier.emit('configuration_reload_failed', error_type=type(exc).__name__)
        self.login.tick()

    def blocked(self, flame):
        return self.configuration_blocked or self.login.blocked(flame)

    def close(self):
        if self.owned_browser is not None:
            self.owned_browser.close()


def build_runtime(
    config_path: str | Path | None = None, *, stream: TextIO | None = None,
    delivery_notifier: Notifier | None = None,
    browser: IsolatedBrowser | OwnedBrowserSession | BrowserPool | None = None,
) -> Runtime:
    config = load_config(config_path)
    storage = SQLiteStorage(config.storage.path)
    notifier = JsonlNotifier(stream)
    clock = SystemClock()
    registry = EntryPointKeeperRegistry()
    for keeper_name in {flame.keeper for flame in config.enabled_flames()}:
        registry.resolve(keeper_name)
    owned_browser = BrowserPool() if browser is None else None
    browser_capability = browser if browser is not None else owned_browser
    login = LoginCoordinator(config, storage, registry, browser_capability, clock=clock)
    orchestrator = Orchestrator(
        storage=storage,
        registry=registry,
        capabilities=CapabilityRegistry({"http": UrllibHttpClient(), "browser": browser_capability}),
        secrets=FileSecretResolver(),
        notifier=delivery_notifier if delivery_notifier is not None else notifier,
        clock=clock,
    )
    service = SchedulerService(
        config=config,
        storage=storage,
        orchestrator=orchestrator,
        clock=clock,
        before_tick=login.tick,
        blocked=login.blocked,
    )
    runtime = Runtime(
        config=config,
        storage=storage,
        notifier=notifier,
        orchestrator=orchestrator,
        service=service,
        owned_browser=owned_browser,
        login=login,
    )
    service.before_tick = runtime.poll_controls
    service.blocked = runtime.blocked
    return runtime
