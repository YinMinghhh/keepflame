"""Composition root for concrete KeepFlame runtime resources."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from keepflame.capabilities import CapabilityRegistry, FileSecretResolver, UrllibHttpClient
from keepflame.capabilities.browser import IsolatedBrowser, OwnedBrowserSession
from keepflame.capabilities.browser_pool import BrowserPool
from keepflame.login import LoginCoordinator
from keepflame.core.models import BusinessTarget
from keepflame.core.config import AppConfig, ConfigError, load_config
from keepflame.core.orchestrator import Orchestrator, SystemClock
from keepflame.core.plugins import EntryPointKeeperRegistry
from keepflame.core.protocols import Notifier
from keepflame.core.service import SchedulerService
from keepflame.core.protocols import AutomationPaused
from keepflame.notifier import JsonlNotifier
from keepflame.notifier.lifecycle import ConfigView, LifecycleNotifier
from keepflame.storage import SQLiteStorage


@dataclass(slots=True)
class Runtime:
    config: AppConfig
    storage: SQLiteStorage
    notifier: JsonlNotifier
    orchestrator: Orchestrator
    service: SchedulerService
    config_view: ConfigView | None = None
    owned_browser: BrowserPool | None = None
    login: LoginCoordinator | None = None
    configuration_blocked: bool = False
    dispatch_owner: str | None = None
    _closed: bool = False
    _close_reason: str = "program_exit"

    @contextmanager
    def daemon_session(self):
        """One owner holds the runner lock until its browser has closed.

        Only an explicitly executing daemon opens a request generation. Ordinary
        CLI clients neither own nor acquire its browser and cannot revive it.
        """
        if self._closed or self.dispatch_owner is not None:
            raise ConfigError('runtime cannot start another daemon session')
        with self.storage.execution_lock():
            owner = str(uuid4())
            with self.storage.daemon_owner_lock(owner):
                self.dispatch_owner = owner
                try:
                    self.storage.start_dispatch(owner, self.config, self.orchestrator.clock.now())
                    self.notifier.emit('daemon_owner_started', owner_id=owner)
                    yield
                except BaseException:
                    self._close_reason = "owner_error"
                    raise
                finally:
                    self.close()

    def request_stop(self):
        self._close_reason = "user_stop"
        self.service.stop_event.set()

    def _browser_stopped(self):
        if self.owned_browser is not None and self.owned_browser.closed:
            self.service.stop_event.set()
            return True
        return False

    def poll_controls(self):
        if self._closed or self.service.stop_event.is_set() or self._browser_stopped():
            return
        # Edits cannot silently leave old enabled tasks running. Only a valid,
        # explicit reload can clear this latch, including after the file is fixed.
        try:
            with self.config.source_path.open("rb") as handle:
                content = handle.read(1_048_577)
            if (len(content) > 1_048_576 or
                    hashlib.sha256(content).hexdigest() != self.config.source_fingerprint):
                self.configuration_blocked = True
        except OSError:
            self.configuration_blocked = True
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
                if self.config_view is not None:
                    self.config_view.config = updated
                self.login.config = updated
                if self.dispatch_owner is not None:
                    self.storage.refresh_dispatch(self.dispatch_owner, updated)
                self.configuration_blocked = False
                self.storage.finish_command(request['id'])
                self.notifier.emit('configuration_reloaded', request_id=request['id'])
            except Exception as exc:
                # Preserve the existing context and action ledger but stop task dispatch.
                self.configuration_blocked = True
                self.storage.finish_command(request['id'], type(exc).__name__)
                self.notifier.emit('configuration_reload_failed', error_type=type(exc).__name__)
        self.login.tick()
        if self._browser_stopped():
            return
        self.dispatch_one()
        self._browser_stopped()

    def dispatch_one(self):
        if self.dispatch_owner is None or self._closed or self.service.stop_event.is_set():
            return
        now = self.orchestrator.clock.now()
        request = self.storage.claim_run(self.dispatch_owner, now)
        if request is None:
            return
        flame = next((f for f in self.config.enabled_flames()
                      if (f.id, f.keeper, f.account_id) ==
                      (request['flame_id'], request['keeper'], request['account_id'])), None)
        reason = None
        target = (None if request['target_day'] is None or request['target_timezone'] is None else
                  BusinessTarget(request['target_day'], request['target_timezone']))
        if target is None or not target.contains(now):
            reason = 'business_day_changed'
        elif flame is not None and target.timezone != flame.schedule.timezone:
            reason = 'business_timezone_changed'
        elif self.configuration_blocked or request['fingerprint'] != self.config.source_fingerprint:
            reason = 'configuration_changed'
        elif flame is None:
            reason = 'flame_disabled_or_changed'
        elif self.blocked(flame) or self.storage.is_paused(flame.keeper, flame.account_id, flame.id):
            reason = 'automation_paused'
        forced = bool(request.get('forced'))
        scheduled_due = None
        if reason is None:
            # Computing due also persists today's window draw exactly as the scheduler would.
            due = self.service.planner.due(self.config, flame, self.storage.get_state(flame.id, flame.account_id), now)
            if due > now:
                if forced:
                    # --force bypasses only this due-time gate; every check above still applies.
                    scheduled_due = due
                else:
                    reason = 'not_due'
        outcome = None
        if reason is None:
            self.notifier.emit('daemon_run_started', request_id=request['id'], owner_id=self.dispatch_owner,
                               flame_id=flame.id, business_target=target.to_dict(), forced=forced,
                               forced_ahead_of_schedule=scheduled_due is not None,
                               scheduled_due=None if scheduled_due is None else scheduled_due.isoformat())
            try:
                outcome = self.orchestrator.run_flame(flame, notify=False, business_target=target,
                                                      forced=forced, scheduled_due=scheduled_due)
            except AutomationPaused:
                # A pause may have arrived after the previous local check.
                reason = 'automation_paused'
            # Unexpected storage/control errors escape and stop the owner. Its
            # running request becomes interrupted; it is never auto-replayed.
        self.storage.finish_run_request(request['id'], self.dispatch_owner,
                                        self.orchestrator.clock.now(), reason=reason, outcome=outcome)
        self.notifier.emit('daemon_run_finished', request_id=request['id'],
                           state='rejected' if reason else 'completed', reason=reason, forced=forced,
                           run_id=None if outcome is None else outcome.record.run_id,
                           result_status=None if outcome is None else outcome.record.result.status.value,
                           business_target=None if target is None else target.to_dict(),
                           run_started_at=None if outcome is None else outcome.record.started_at.isoformat(),
                           run_finished_at=None if outcome is None else outcome.record.finished_at.isoformat(),
                           completion_receipt=(None if outcome is None or outcome.record.result.receipt is None else
                                               outcome.record.result.receipt.to_dict()))

    def blocked(self, flame):
        # A preceding scheduled Flame may have stopped this owner after the
        # tick's control poll. Check before every subsequent Keeper admission.
        return (self._closed or self.service.stop_event.is_set() or self._browser_stopped()
                or self.configuration_blocked or self.login.blocked(flame)
                or self.dispatch_owner is not None
                and self.storage.has_requested_run(self.dispatch_owner, flame))

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.service.stop_event.set()
        owner = self.dispatch_owner
        self.dispatch_owner = None
        try:
            if owner is not None:
                self.storage.stop_dispatch(owner, self.orchestrator.clock.now())
        finally:
            if self.owned_browser is not None:
                self.owned_browser.close(reason=self._close_reason)
        if owner is not None:
            self.notifier.emit('daemon_owner_stopped', owner_id=owner,
                               close_reason=(self.owned_browser.close_reason if self.owned_browser is not None
                                             else self._close_reason),
                               unconfirmed_actions_preserved=True)


def build_runtime(
    config_path: str | Path | None = None, *, stream: TextIO | None = None,
    delivery_notifier: Notifier | None = None,
    browser: IsolatedBrowser | OwnedBrowserSession | BrowserPool | None = None,
    restore_browser_session: bool = False,
    mail_connect=None,
    slack_post=None,
) -> Runtime:
    if restore_browser_session and browser is not None:
        raise ConfigError("browser session restoration requires the runtime-owned browser")
    config = load_config(config_path)
    storage = SQLiteStorage(config.storage.path)
    notifier = JsonlNotifier(stream)
    config_view = ConfigView(config)
    secrets = FileSecretResolver()
    lifecycle = LifecycleNotifier(
        notifier, config_view, secrets, connect=mail_connect, slack_post=slack_post,
    )
    clock = SystemClock()
    registry = EntryPointKeeperRegistry()
    for keeper_name in {flame.keeper for flame in config.enabled_flames()}:
        registry.resolve(keeper_name)
    owned_browser = ((BrowserPool(IsolatedBrowser(restore_session=True))
                      if restore_browser_session else BrowserPool()) if browser is None else None)
    browser_capability = browser if browser is not None else owned_browser
    login = LoginCoordinator(config, storage, registry, browser_capability, clock=clock)
    orchestrator = Orchestrator(
        storage=storage,
        registry=registry,
        capabilities=CapabilityRegistry({"http": UrllibHttpClient(), "browser": browser_capability}),
        secrets=secrets,
        notifier=delivery_notifier if delivery_notifier is not None else lifecycle,
        clock=clock,
        config_path=str(config.source_path.resolve()),
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
        config_view=config_view,
        owned_browser=owned_browser,
        login=login,
    )
    service.before_tick = runtime.poll_controls
    service.blocked = runtime.blocked
    return runtime
