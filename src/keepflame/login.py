"""Local operator handoff consumed only by the running daemon.

The plugin supplies login URLs and excluded task routes. Confirming login does
not assert authentication; the next Keeper inspection must verify it remotely.
"""
from datetime import datetime, timezone


class LoginCoordinator:
    def __init__(self, config, storage, registry, browser, *, clock):
        self.config, self.storage, self.registry = config, storage, registry
        self.browser, self.clock = browser, clock
        self.active = {}

    def blocked(self, flame):
        return any(r['keeper'] == flame.keeper and r['account_id'] == flame.account_id
                   for r in self.storage.login_requests())

    def tick(self):
        now = self.clock.now().astimezone(timezone.utc)
        requests = self.storage.login_requests()
        live_ids = {request['id'] for request in requests}
        for identifier in list(self.active):
            if identifier not in live_ids:
                self.browser.end_login(self.active.pop(identifier))
        for request in requests:
            identifier, state = request['id'], request['state']
            flame = next((f for f in self.config.flames if f.id == request['flame_id']
                          and f.keeper == request['keeper'] and f.account_id == request['account_id']), None)
            profile = self.active.get(identifier)
            if datetime.fromisoformat(request['expires_at']) <= now or flame is None:
                self._finish(request, 'expired', profile)
                continue
            if state == 'requested':
                self.storage.pause(flame.keeper, flame.account_id, '*', 'login_requested', now)
                try:
                    keeper = self.registry.resolve(flame.keeper)
                    profile = flame.settings['browser_profile_dir']
                    if not isinstance(profile, str) or not profile:
                        raise ValueError('dedicated browser profile required')
                    self.browser.begin_login(profile_dir=profile, account_id=flame.account_id,
                                             origin=keeper.browser_origin, login_url=keeper.login_url,
                                             blocked_paths=keeper.login_blocked_paths)
                    self.active[identifier] = profile
                    self.storage.transition_login(identifier, 'requested', 'waiting')
                except Exception:
                    self._finish(request, 'failed', profile)
            elif state == 'waiting':
                if profile is None:
                    self._finish(request, 'interrupted', None)
                    continue
                try:
                    self.browser.pump_login(profile)
                except Exception:
                    self._finish(request, 'failed', profile)
            elif state in ('confirmed', 'cancelled'):
                valid_confirmation = state == 'confirmed' and profile is not None
                self._finish(request, 'completed' if valid_confirmation else 'closed', profile)
                if valid_confirmation:
                    self.storage.resume(flame.keeper, flame.account_id, '*')
                    for target in self.config.flames:
                        if (target.keeper, target.account_id) == (flame.keeper, flame.account_id):
                            self.storage.request_inspection(target.id, target.account_id, now)

    def _finish(self, request, state, profile):
        if profile is not None:
            self.browser.end_login(profile)
        self.active.pop(request['id'], None)
        self.storage.transition_login(request['id'], request['state'], state)
