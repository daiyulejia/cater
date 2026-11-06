"""Read-only formal review evidence for one delivered version.

Input must be a trusted, complete, consistent snapshot of *one task's* events,
including the task creation and all events through delivery. This function does
not authenticate the log, prove snapshot completeness, or authorize delivery.
Counts describe recorded conclusions and artifact-set-change resubmissions, not
content changes, actual rework, or proof that feedback caused or fixed a change.
Only structurally valid chains whose latest submitted artifact references are
all in the delivery contribute counts. A chain containing a delivered older reference but
whose latest set is not fully delivered makes the result incomplete.
For relevance, a validated publication with the same filename and content hash
counts even if its sequence differs; that match alone never admits a chain into
the counts. Every reference still has to match its own publication event.
"""
from __future__ import annotations

import re

from .events import Actor
from .team_review_direction import direction_at, effective_objectives


HOST = Actor.system('team')
_SHA = re.compile(r'^[0-9a-f]{64}$')


def _positive(n):
    return type(n) is int and n > 0


def _refs(value, by_seq, before, *, allow_empty=False):
    if not isinstance(value, list) or (not value and not allow_empty) or len(value) > 128:
        return None
    out = []
    for ref in value:
        if (type(ref) is not dict or set(ref) != {'seq', 'filename', 'content_sha256'}
                or not _positive(ref['seq']) or ref['seq'] >= before
                or not isinstance(ref['filename'], str) or not ref['filename']
                or not isinstance(ref['content_sha256'], str) or not _SHA.fullmatch(ref['content_sha256'])):
            return None
        event = by_seq.get(ref['seq'])
        if (event is None or event.type != 'deliverable.published' or event.actor.kind != 'agent'
                or event.payload.get('filename') != ref['filename']
                or event.payload.get('content_sha256') != ref['content_sha256']):
            return None
        out.append((ref['seq'], ref['filename'], ref['content_sha256']))
    if len({x[0] for x in out}) != len(out) or len({x[1] for x in out}) != len(out):
        return None
    return tuple(sorted(out))


def project_review_delivery_stats(events, *, task_id, delivery_seq):
    """Return conservative counts and event references, or an incomplete result.

    Caller must independently check the task and delivery are user-visible and
    pass a consistent, complete task snapshot. No customer or feedback text is
    returned. Events later than the delivery are deliberately ignored.
    """
    result = {'delivery_seq': delivery_seq, 'complete': False, 'review_conclusions': 0,
              'changes_requested': 0, 'artifact_set_change_resubmissions': 0, 'chains': []}
    if not isinstance(task_id, str) or not task_id or not _positive(delivery_seq):
        return result
    events = list(events)
    if (any(e.task_id != task_id or not _positive(e.seq) for e in events)
            or len({e.seq for e in events}) != len(events)):
        return result
    events = sorted((e for e in events if e.seq <= delivery_seq), key=lambda e: e.seq)
    by_seq = {e.seq: e for e in events}
    delivery = by_seq.get(delivery_seq)
    created = [e for e in events if e.type == 'task.created' and e.actor == Actor.human()]
    effective = effective_objectives(events)
    if (len(created) != 1 or delivery is None or delivery.type != 'team.objective_completed'
            or delivery.actor != HOST or delivery.seq not in effective):
        return result
    delivered = _refs(delivery.payload.get('artifacts'), by_seq, delivery_seq, allow_empty=True)
    direction = direction_at(events, delivery_seq)
    if delivered is None or direction is None:
        return result
    def revision_before(seq):
        return max((e.seq for e in events if e.seq < seq and e.seq in effective
                    and e.type in ('team.objective_updated', 'team.objective_completed')), default=0)

    delivery_revision = revision_before(delivery_seq)
    delivered_set = set(delivered)
    delivered_content = {(filename, digest) for _, filename, digest in delivered}
    requests, decisions, incomplete = {}, {}, False
    for event in events:
        p = event.payload
        if event.type == 'team.review_requested':
            if event.actor != HOST:
                incomplete = True
                continue
            refs = _refs(p.get('artifacts'), by_seq, event.seq)
            root = p.get('root_seq') if p.get('root_seq') is not None else event.seq
            if (refs is None or not _positive(root) or root > event.seq
                    or not isinstance(p.get('author'), str) or not p['author']
                    or not isinstance(p.get('reviewer'), str) or not p['reviewer']
                    or p['author'] == p['reviewer'] or type(p.get('revision')) is not int
                    or p['revision'] < 0 or p.get('owner', p['author']) != p['author']):
                incomplete = True
                continue
            requests[event.seq] = (event, refs, root)
        elif event.type == 'team.review_recorded':
            if event.actor != HOST or not _positive(p.get('review_seq')):
                incomplete = True
                continue
            decisions.setdefault(p['review_seq'], []).append(event)
    groups = {}
    for req, refs, root in requests.values():
        groups.setdefault(root, []).append((req, refs))
    if any(seq not in requests for seq in decisions):
        incomplete = True
    for root, chain in groups.items():
        latest = chain[-1][0]
        if (direction_at(events, latest.seq) == direction
                and latest.payload['revision'] == delivery_revision):
            # A current-scope unresolved or contradictory review must remain
            # visible even when its artifacts were not selected for delivery.
            for req, _ in chain:
                ds = decisions.get(req.seq, [])
                if (len(ds) != 1 or ds[0].seq <= req.seq
                        or ds[0].payload.get('root_seq') != root
                        or ds[0].payload.get('reviewer') != req.payload['reviewer']
                        or ds[0].payload.get('verdict') not in ('approved', 'changes_requested')):
                    incomplete = True
            last_decisions = decisions.get(latest.seq, [])
            if len(last_decisions) == 1 and last_decisions[0].payload.get('verdict') == 'changes_requested':
                incomplete = True
        # Do not present a complete result when the delivery contains a prior
        # member of a chain whose latest submission is not fully delivered.
        # This includes pending or conflicting newer review outcomes.
        if not set(chain[-1][1]) <= delivered_set:
            if any((filename, digest) in delivered_content
                   for _, refs in chain for _, filename, digest in refs):
                incomplete = True
            continue
        if chain[0][0].seq != root:
            incomplete = True
            continue
        review_count = changes_count = resubmissions = 0
        refs_out = []
        valid = True
        for i, (req, refs) in enumerate(chain):
            p = req.payload
            if (direction_at(events, req.seq) != direction or p['revision'] != revision_before(req.seq)
                    or p['revision'] != delivery_revision):
                valid = False
                break
            if i:
                prev, prev_refs = chain[i - 1]
                prev_decisions = decisions.get(prev.seq, [])
                if (p.get('previous_review_seq') != prev.seq
                        or p.get('previous_artifacts') != prev.payload.get('artifacts')
                        or any(p.get(k) != prev.payload.get(k) for k in ('author', 'reviewer', 'revision'))
                        or len(prev_decisions) != 1 or prev_decisions[0].seq >= req.seq
                        or not any(ref[0] > prev_decisions[0].seq for ref in refs)):
                    valid = False
                    break
                if prev_decisions[0].payload.get('verdict') == 'changes_requested':
                    old_versions = {(x[1], x[2]) for x in prev_refs}
                    new_versions = {(x[1], x[2]) for x in refs}
                    if new_versions != old_versions:
                        if any(x[0] > prev_decisions[0].seq and (x[1], x[2]) not in old_versions for x in refs):
                            resubmissions += 1
                        else:
                            # Removal or an earlier unrelated version is not
                            # evidence of a newly published changed set member.
                            incomplete = True
            elif p.get('previous_review_seq') is not None:
                valid = False
                break
            ds = decisions.get(req.seq, [])
            if len(ds) > 1:
                valid = False
                break
            if not ds:
                incomplete = True
            if ds:
                d = ds[0]
                if (d.seq <= req.seq or d.payload.get('root_seq') != root
                        or d.payload.get('reviewer') != p['reviewer']
                        or d.payload.get('verdict') not in ('approved', 'changes_requested')):
                    valid = False
                    break
                review_count += 1
                changes_count += d.payload['verdict'] == 'changes_requested'
                refs_out.append({'request_seq': req.seq, 'decision_seq': d.seq})
        if not valid:
            incomplete = True
            continue
        result['review_conclusions'] += review_count
        result['changes_requested'] += changes_count
        result['artifact_set_change_resubmissions'] += resubmissions
        result['chains'].append({'root_seq': root, 'decisions': refs_out})
    result['complete'] = not incomplete
    return result
