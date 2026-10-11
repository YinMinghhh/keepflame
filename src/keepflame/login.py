"""Local operator handoff consumed only by the running daemon.

The plugin supplies login URLs, the task-info proof, and excluded task routes.
A saved session is GET task-info code 00000 while the login window is open.
The 15-minute request does not close that window.
"""
from datetime import datetime, timezone


_LOGIN_PAUSE_REASONS = frozenset({"login_requested", "login_required"})


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
            expired = datetime.fromisoformat(request['expires_at']) <= now
            window_open = self._window_open(profile)
            # A closed window can expire. An open one stays up so the user can
            # still log in, and task-info proof clears the login pause.
            if flame is None or (expired and not window_open):
                self._finish(request, 'expired', profile)
                continue
            if state == 'requested':
                self.storage.pause(flame.keeper, flame.account_id, '*', 'login_requested', now)
                if not self.storage.transition_login(identifier, 'requested', 'opening'):
                    continue
                request = {**request, 'state': 'opening'}
                try:
                    keeper = self.registry.resolve(flame.keeper)
                    profile = flame.settings['browser_profile_dir']
                    if not isinstance(profile, str) or not profile:
                        raise ValueError('dedicated browser profile required')
                    self.browser.begin_login(profile_dir=profile, account_id=flame.account_id,
                                             origin=keeper.browser_origin, login_url=keeper.login_url,
                                             blocked_paths=keeper.login_blocked_paths)
                    self.active[identifier] = profile
                    self.storage.transition_login(identifier, 'opening', 'waiting')
                except Exception:
                    self._finish(request, 'failed', profile)
            elif state == 'opening':
                self._finish(request, 'interrupted', profile)
            elif state == 'waiting':
                if profile is None:
                    self._finish(request, 'interrupted', None)
                    continue
                try:
                    self.browser.pump_login(profile)
                except Exception:
                    self._finish(request, 'failed', profile)
                    continue
                self._accept_if_proven(request, flame, profile, now)
            elif state in ('confirmed', 'cancelled'):
                valid_confirmation = state == 'confirmed' and profile is not None
                self._finish(request, 'completed' if valid_confirmation else 'closed', profile)
                if valid_confirmation:
                    self._clear_login_pause(flame)
                    self._due_account(flame, now)

    def _window_open(self, profile):
        if profile is None:
            return False
        checker = getattr(self.browser, "login_window_open", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(profile))
        except Exception:
            return False

    def _accept_if_proven(self, request, flame, profile, now):
        if self._login_proven(flame, profile) is not True:
            return False
        self._clear_login_pause(flame)
        self._due_account(flame, now)
        self._finish(request, 'completed', profile)
        return True

    def _login_proven(self, flame, profile):
        try:
            keeper = self.registry.resolve(flame.keeper)
        except Exception:
            return None
        url = getattr(keeper, "login_proof_url", None)
        interpret = getattr(keeper, "login_proven", None)
        reader = getattr(self.browser, "read_login_proof", None)
        if not isinstance(url, str) or not callable(interpret) or not callable(reader):
            return None
        try:
            read = reader(profile, url)
        except Exception:
            return None
        if not isinstance(read, tuple) or len(read) != 2:
            return None
        try:
            return interpret(read[0], read[1])
        except Exception:
            return None

    def _clear_login_pause(self, flame):
        reason = getattr(self.storage, "pause_reason", None)
        if not callable(reason):
            return
        if reason(flame.keeper, flame.account_id, "*") in _LOGIN_PAUSE_REASONS:
            self.storage.resume(flame.keeper, flame.account_id, "*")

    def _due_account(self, flame, now):
        for target in self.config.enabled_flames():
            if (target.keeper, target.account_id) == (flame.keeper, flame.account_id):
                self.storage.request_inspection(target.id, target.account_id, now)

    def _finish(self, request, state, profile):
        if profile is not None:
            self.browser.end_login(profile)
        self.active.pop(request['id'], None)
        self.storage.transition_login(request['id'], request['state'], state)
