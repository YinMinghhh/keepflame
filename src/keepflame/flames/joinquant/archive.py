"""Offline review of recorded guard evidence, never a remote status query."""
import json

from keepflame.core.protocols import ArchiveError
from .archive_cases import enforce_known_reading_entry_case, review_reading_entry_case

# That historical executable's counters were reviewed against its pinned source:
# each admitted write increments before forward(), and final counts follow cleanup.
LEGACY_GUARD_REVISION = 'acbe79bc24d26cf5740dacb588b8605604007510'
WRITE_COUNTERS = ('reading_registration', 'challenge', 'answer', 'claim')


def review_abandonment(action, run, evidence):
    # Every Keeper/CLI/storage caller reaches this binding before reason routing.
    # Known intents cannot fall back to a legacy policy after a recorded-row change.
    enforce_known_reading_entry_case(action, run, evidence)
    metadata = json.loads(run['metadata_json'])
    counters = metadata.get('stage_counts', {})
    reading_entry = metadata.get('reason') == 'reading_button_changed'
    if (action['task'] not in {'reading', 'checkin'}
            or run['status'] not in {'FAILED', 'WAITING_HUMAN'}
            or run['reward_json'] is not None
            or (not reading_entry and metadata.get('reason') not in {'unexpected_write', 'business_day_changed',
                'article_navigation_changed', 'article_not_public', 'reading_registration_unconfirmed'})
            or metadata.get('task') != action['task']
            or metadata.get('execution_mode') != 'browser'
            or metadata.get('action_stage') not in {'reading', 'claim'}
            or metadata.get('automation_paused') is not True
            or not isinstance(counters, dict)
            or any(type(counters.get(key)) is not int or counters[key] != 0 for key in WRITE_COUNTERS)):
        raise ArchiveError('recorded run does not prove all guarded mutations were unadmitted')
    if reading_entry:
        if (action['keeper'] != 'joinquant' or action['task'] != 'reading'
                or action['state'] != 'pending' or action['confirmed_at'] is not None
                or run['status'] != 'FAILED' or run.get('completion_receipt_json') is not None
                or type(run.get('clock_regressed')) is not int or run['clock_regressed'] != 0
                or metadata.get('action_stage') != 'claim'
                or type(metadata.get('write_guard_schema')) is not int or metadata['write_guard_schema'] != 1):
            raise ArchiveError('reading entry case lacks supported zero-write failure evidence')
        review_reading_entry_case(action, run, evidence)
        return
    if not (type(metadata.get('write_guard_schema')) is int and metadata['write_guard_schema'] == 1) and evidence['source_revision'] != LEGACY_GUARD_REVISION:
        raise ArchiveError('guard counter semantics lack a supported reviewed source identity')
    # A missing first-rejection path cannot be invented here. The pause remains;
    # explicit separate pause review and normal live eligibility checks still apply.
