"""One immutable business date for an in-flight reward attempt."""
from datetime import timedelta
from email.utils import parsedate_to_datetime
import time
from zoneinfo import ZoneInfo

from keepflame.flames.joinquant.guard import FlowError
from keepflame.core.models import BusinessTarget

ZONE = ZoneInfo('Asia/Shanghai')


class BusinessDayFence:
    def __init__(self, context):
        self.started = context.now
        self.target = getattr(context, 'business_target', None) or BusinessTarget.at(context.now, ZONE.key)
        if self.target.timezone != ZONE.key or not self.target.contains(context.now):
            raise FlowError('business_target_mismatch')
        self.day = self.target.day
        self.clock = getattr(context, 'clock', None)
        self.monotonic_start = time.monotonic()
        self.changed = False

    def check(self):
        # Runtime supplies its live clock. Standalone Contexts still advance with
        # elapsed monotonic time instead of treating context.now as a frozen clock.
        now = (self.clock.now() if self.clock is not None else
               self.started + timedelta(seconds=time.monotonic()-self.monotonic_start))
        if now.tzinfo is None or now.utcoffset() is None:
            self.changed = True
        elif now.astimezone(ZONE).date().isoformat() != self.day:
            self.changed = True
        if self.changed:
            raise FlowError('business_day_changed')
        return now

    def response(self, headers):
        self.check()
        value = headers.get('date')
        if value is not None:
            try:
                remote = parsedate_to_datetime(value)
                same = remote.tzinfo is not None and remote.astimezone(ZONE).date().isoformat() == self.day
            except (TypeError, ValueError, OverflowError):
                same = False
            if not same:
                self.changed = True
                raise FlowError('business_day_changed')
