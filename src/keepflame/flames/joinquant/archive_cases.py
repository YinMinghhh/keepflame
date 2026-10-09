"""Reviewed historical exceptions; no generic zero-counter entry-failure waiver."""
from dataclasses import dataclass
import hashlib
import json

from keepflame.core.protocols import ArchiveError


def snapshot_sha256(value):
    # Hash the complete storage row, including its original serialized JSON fields.
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


def intent_sha256(action):
    # Stable reservation identity: state/confirmation and run metadata are not
    # routing inputs. A changed row for this intent must still receive full review.
    return snapshot_sha256({key: action.get(key) for key in
                            ('keeper', 'account_id', 'task', 'day', 'started_at')})


def run_id_sha256(run):
    # A second independent anchor catches changed action fields for the same run.
    return snapshot_sha256({'run_id': run.get('run_id')})


@dataclass(frozen=True)
class ReadingEntryCase:
    intent_sha256: str
    run_id_sha256: str
    certificate_sha256: str
    action_sha256: str
    run_sha256: str
    source_revision: str
    references: frozenset[str]


# One already audited event, bound to its intent/account/day/run and repair
# candidate through a pinned certificate. No account identifiers or raw evidence
# are stored here. Adding a different case requires another source/evidence review.
# Certificate e3deb491... binds executed source 40e607c and reviewed entry repair
# 599277d / tree aaeb506f34f31566a0462deb10b8f7b3f960d650. It is NOT permission
# to modify real state, remove a pause or run a task.
REVIEWED_READING_ENTRY_CASES = (
    ReadingEntryCase(
        intent_sha256='ef91f2fc265671e0b9b8451daf8fe27d6dfde05baf02a1c849d200b149ba7a7c',
        run_id_sha256='5c799f80b3692cb341d8059132d81ffc5b9b64ae130fe477ba79f83d3436d81f',
        certificate_sha256='e3deb491d9ce58bf859ae245b0503ea254577ab5b3b7fb040e4ad0050ce1ec44',
        action_sha256='b9ab244e42dbbc1454be2515e53f5bb39401a01a477226cd3727e6dcc1a43630',
        run_sha256='ed2e90f4402bb52ae18ff0df7d4f53072037d5f761f0dffbefbc9045b632fe50',
        source_revision='40e607c05780bc41ff8f7f660e4bfe94d43828ab',
        references=frozenset({
            '1ec92652c909bb59a36d80ccc919b374fda5ffaed0519fb31eaac0493f0191e6',
            '57a6f65ca66c1aecd11c8c4672cc4ac42842ae3506a8b25625040eea2010db89',
            '6c99005f66a8f74f725769016f76fc05f435d24bf866d9dcecc1e46616664358',
            '8768160c57cd1903dc3f87b65bc4b94d8114c3a626d36ad5f74cc8f4353e59b5',
            'e3deb491d9ce58bf859ae245b0503ea254577ab5b3b7fb040e4ad0050ce1ec44',
        }),
    ),
)


def enforce_known_reading_entry_case(action, run, evidence):
    """Check known identities before any mutable metadata can select a policy."""
    intent_id, run_id = intent_sha256(action), run_id_sha256(run)
    refs = evidence.get('references', []) if isinstance(evidence, dict) else []
    if any(case.intent_sha256 == intent_id or case.run_id_sha256 == run_id
           or (isinstance(refs, list) and case.certificate_sha256 in refs)
           for case in REVIEWED_READING_ENTRY_CASES):
        review_reading_entry_case(action, run, evidence)


def review_reading_entry_case(action, run, evidence):
    if (not isinstance(evidence, dict)
            or set(evidence) != {'version', 'source_revision', 'references'}
            or type(evidence['version']) is not int or evidence['version'] != 1
            or not isinstance(evidence['references'], list)
            or any(not isinstance(ref, str) for ref in evidence['references'])
            or len(set(evidence['references'])) != len(evidence['references'])):
        raise ArchiveError('reading entry evidence must identify one reviewed case')
    intent_id, run_id = intent_sha256(action), run_id_sha256(run)
    action_digest, run_digest = snapshot_sha256(action), snapshot_sha256(run)
    if not any(case.intent_sha256 == intent_id and case.run_id_sha256 == run_id
               and case.action_sha256 == action_digest and case.run_sha256 == run_digest
               and case.source_revision == evidence['source_revision']
               and case.references == frozenset(evidence['references'])
               for case in REVIEWED_READING_ENTRY_CASES):
        raise ArchiveError('reading entry evidence is not bound to this exact reviewed intent and run')
