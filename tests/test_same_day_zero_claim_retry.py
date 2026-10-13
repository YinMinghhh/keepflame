"""Same-day retry for a pending that sent no challenge, answer, or claim."""

from datetime import timedelta
from types import SimpleNamespace

from keepflame.core.models import KeepStatus
from keepflame.storage import SQLiteStorage
from tests.test_action_day_identity import STARTED, _seed_checkin
from tests.test_joinquant_reading import JournalStorage, reading
from tests.test_reading_rotation import A, B, run

LATER = STARTED + timedelta(minutes=5)
DAY = '2026-10-10'


def _store(tmp_path, name, **counts):
    store = SQLiteStorage(tmp_path / f'{name}.sqlite3')
    _seed_checkin(store, counts.pop('claim'), **counts)
    return store


def test_zero_claim_same_day_pending_is_retryable_and_restamped(tmp_path):
    store = _store(tmp_path, 'zero', claim=0)
    assert store.zero_claim_pending_retryable('joinquant', 'primary', 'checkin', DAY) is True
    assert store.reserve_action('joinquant', 'primary', 'checkin', DAY, LATER) is True
    action = store.get_action('joinquant', 'primary', 'checkin')
    assert action.state == 'pending' and action.day == DAY and action.started_at == LATER


def test_submitted_writes_keep_the_same_day_duplicate_guard(tmp_path):
    for name, counts in {'claim': dict(claim=1), 'challenge': dict(claim=0, challenge=1),
                         'answer': dict(claim=0, answer=1)}.items():
        store = _store(tmp_path, name, **counts)
        assert store.zero_claim_pending_retryable('joinquant', 'primary', 'checkin', DAY) is False
        assert store.reserve_action('joinquant', 'primary', 'checkin', DAY, LATER) is False


def test_missing_counts_or_confirmed_same_day_stay_blocked(tmp_path):
    missing = SQLiteStorage(tmp_path / 'missing.sqlite3')
    _seed_checkin(missing, 0, record_run=False)
    assert missing.reserve_action('joinquant', 'primary', 'checkin', DAY, LATER) is False
    done = _store(tmp_path, 'done', claim=0)
    assert done.confirm_action('joinquant', 'primary', 'checkin', LATER, day=DAY)
    assert done.reserve_action('joinquant', 'primary', 'checkin', DAY, LATER) is False


class RetryStorage(JournalStorage):
    """Today's zero-claim pending exists; the storage rule permits a retry."""

    def __init__(self, day, *, retryable=True):
        super().__init__()
        self.day, self.retryable = day, retryable
        self.actions[('joinquant', 'synthetic', 'reading')] = SimpleNamespace(
            state='pending', day=day, started_at=None, confirmed_at=None)
        self.read, self.recorded = {'111'}, []

    def zero_claim_pending_retryable(self, keeper, account_id, task, day):
        return self.retryable and day == self.day

    def reserve_action(self, keeper, account_id, task, day, now, *, validate_time=None):
        item = self.actions[(keeper, account_id, task)]
        if not (self.retryable and item.state == 'pending' and item.day == day):
            return False
        item.started_at = now
        self.reservations += 1
        return True

    def read_article_ids(self, keeper, account_id):
        return set(self.read)

    def record_article_read(self, keeper, account_id, article_id, day, now):
        self.recorded.append(article_id)


SETTINGS = {'task': 'reading', 'article_urls': [A, B], 'browser_profile_dir': '/unused', 'mode': 'browser'}


def _today():
    from tests.test_orchestrator import local
    return local(11, 9).date().isoformat()


def test_same_day_claimable_pending_goes_on_to_claim():
    storage = RetryStorage(_today())
    result, page, _ = run(SETTINGS, storage, [reading({'status': 0, 'totalCount': 1}), reading({'status': 1})])
    assert result.status is KeepStatus.SUCCESS
    assert page.reads == 0 and page.claims == 1 and storage.reservations == 1


def test_same_day_needs_reading_reads_next_unread_article():
    storage = RetryStorage(_today())
    result, page, task = run(SETTINGS, storage,
                             [reading(), reading({'status': 0, 'totalCount': 1}), reading({'status': 1})])
    assert result.status is KeepStatus.SUCCESS
    assert task.article_id == '222' and page.reads == 1 and storage.recorded == ['222']


def test_same_day_pending_without_retry_permission_stays_unconfirmed():
    storage = RetryStorage(_today(), retryable=False)
    result, page, _ = run(SETTINGS, storage, [reading({'status': 0, 'totalCount': 1})])
    assert result.metadata['reason'] == 'claim_unconfirmed'
    assert page.reads == page.claims == 0
