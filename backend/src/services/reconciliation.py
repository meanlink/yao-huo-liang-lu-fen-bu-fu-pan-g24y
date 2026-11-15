"""双来源对账单：出窑台账 vs 终检表。

业务规则（与 BUSINESS.md 一致）：
1. 双方到齐、逐窑件数相符或差异经裁定，才允许发布分析批次。
2. 缺失窑次与未裁定差异留待处理，不阻塞对账单继续接收修订，但阻塞发布。
3. 每侧按修订号择优合并：迟到旧修订只留痕，不覆盖新值。
4. 某窑的任一侧取值或修订变化，该窑的差异裁定立即失效（stale），需重新裁定。
5. 发布是原子的、幂等的：同一 request_id 或同一内容指纹只产生一个批次；
   源数据变化后发布另开新版本，既有批次与复盘不被改写。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models import (Adjudication, Job, LedgerEntry, PublishedBatch,
                        ReconciliationStatement, SourceSubmission)
from src.services.business import metadata
from src.services.datasets import create as create_dataset
from src.schemas.dataset import DatasetInput
from src.utils.revisions import RevisionKey, compare
from src.utils.serialization import checksum

SIDES = ('ledger', 'inspection')
SIDE_LABELS = {'ledger': '出窑台账', 'inspection': '终检表'}
FIELD_ALIASES = {
    'batch': ('batch', '窑次', '批号', 'kiln_batch'),
    'line': ('line', '窑线', 'kiln_line'),
    'fired': ('fired', '烧成件数', '烧成数', '装窑件数'),
    'passed': ('passed', '合格件数', '合格数'),
    'revision': ('revision', '修订号', '版本号', 'rev'),
}
MAX_ROWS = 5000


# ---------------------------------------------------------------- helpers
def _batch_key(value):
    return tuple((0, int(t)) if t.isdigit() else (1, t.casefold()) for t in re.split(r'(\d+)', value) if t)


def _pick(raw, field):
    for key in FIELD_ALIASES[field]:
        if key in raw:
            return raw[key]
    return None


def _finite(value, label, index):
    try:
        if isinstance(value, bool):
            raise TypeError
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
        return number
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(422, f'第 {index + 1} 行 {label} 必须为有限数值')


def _normalize_records(records, revision):
    """返回 (rows, row_revisions)。允许修订号写在行内，但整次提交必须一致。"""
    if not isinstance(records, list) or not records:
        raise HTTPException(422, '源数据至少需要一条窑次记录')
    if len(records) > MAX_ROWS:
        raise HTTPException(422, f'单次提交不能超过 {MAX_ROWS} 条记录')
    rows, seen = [], set()
    for index, raw in enumerate(records):
        if not isinstance(raw, dict):
            raise HTTPException(422, f'第 {index + 1} 行必须是对象')
        batch = _pick(raw, 'batch')
        batch = str(batch).strip() if batch is not None else ''
        if not batch or len(batch) > 120:
            raise HTTPException(422, f'第 {index + 1} 行缺少有效窑次')
        if batch in seen:
            raise HTTPException(422, f'同一份源表中窑次 {batch} 重复出现')
        seen.add(batch)
        line = _pick(raw, 'line')
        line = str(line).strip()[:80] if line not in (None, '') else ''
        fired = _finite(_pick(raw, 'fired'), '烧成件数', index)
        passed = _finite(_pick(raw, 'passed'), '合格件数', index)
        if fired <= 0 or fired != int(fired):
            raise HTTPException(422, f'第 {index + 1} 行 烧成件数须为正整数')
        if passed < 0 or passed > fired or passed != int(passed):
            raise HTTPException(422, f'第 {index + 1} 行 须满足 0≤合格件数≤烧成件数')
        row_rev = _pick(raw, 'revision')
        rows.append({'batch': batch, 'line': line, 'fired': int(fired), 'passed': int(passed),
                     'revision': str(row_rev).strip() if row_rev not in (None, '') else revision})
    revisions = {row['revision'] for row in rows}
    if len(revisions) != 1:
        raise HTTPException(422, '同一次提交的修订号必须一致，或使用提交级修订号')
    effective = next(iter(revisions))
    if not effective:
        raise HTTPException(422, '源表需要修订号，便于迟到旧记录让位')
    if not revision:
        revision = effective
    elif _norm_rev(revision) != _norm_rev(effective):
        raise HTTPException(422, '提交级修订号与源表行内修订号不一致')
    return rows, revision


def _norm_rev(value):
    return RevisionKey(value) if str(value).strip() else None


def _entry_dict(entry, latest_revision=None):
    if entry is None:
        return None
    carried = latest_revision is not None and entry.revision != latest_revision
    return {'batch': entry.batch, 'line': entry.line, 'fired': entry.fired, 'passed': entry.passed,
            'revision': entry.revision, 'submission_id': entry.submission_id,
            'received_at': entry.received_at, 'carried_forward': carried}


def _evidence(ledger, inspection):
    if ledger is None or inspection is None:
        return {}
    return {'ledger_revision': ledger.revision, 'ledger_fired': ledger.fired,
            'ledger_passed': ledger.passed, 'ledger_line': ledger.line,
            'inspection_revision': inspection.revision, 'inspection_fired': inspection.fired,
            'inspection_passed': inspection.passed, 'inspection_line': inspection.line}


def _evidence_matches(evidence, ledger, inspection):
    """源数据更新（该窑任一来源出到新修订）即让原裁定失效。

    裁定只能在两侧现行修订与取值都和裁定时一致时继续使用；否则留待重新裁定，
    既避免用旧值发布，也避免发布请求与源表修订并发时沿用过期结论。
    """
    if not evidence or ledger is None or inspection is None:
        return False
    for side, entry in (('ledger', ledger), ('inspection', inspection)):
        if evidence.get(f'{side}_revision') != entry.revision:
            return False
        if evidence.get(f'{side}_fired') != entry.fired:
            return False
        if evidence.get(f'{side}_passed') != entry.passed:
            return False
        if evidence.get(f'{side}_line') != entry.line:
            return False
    return True


def _bad(requirement, message, batch=None):
    item = {'code': requirement, 'message': message}
    if batch is not None:
        item['batch'] = batch
    return item


# ---------------------------------------------------------------- statements
def create_statement(db: Session, name: str, user):
    name = name.strip()
    if len(name) < 2:
        raise HTTPException(422, '对账单名称至少需要两个字符')
    if db.scalar(select(ReconciliationStatement).where(ReconciliationStatement.name == name)):
        raise HTTPException(409, '该对账单名称已存在')
    now = time.time()
    statement = ReconciliationStatement(name=name, state='awaiting_sources', created_by=user.email,
                                        created_at=now, updated_at=now, snapshot={})
    db.add(statement)
    db.flush()
    statement.snapshot = _refresh(db, statement)
    db.flush()
    return statement


def _locked_statement(db: Session, statement_id: int) -> ReconciliationStatement:
    statement = db.scalar(select(ReconciliationStatement)
                          .where(ReconciliationStatement.id == statement_id)
                          .with_for_update())
    if statement is None:
        raise HTTPException(404, '对账单不存在')
    return statement


def _submissions(db: Session, statement_id: int):
    return list(db.scalars(select(SourceSubmission)
                           .where(SourceSubmission.statement_id == statement_id)
                           .order_by(SourceSubmission.created_at, SourceSubmission.id)))


def _entries(db: Session, statement_id: int):
    return list(db.scalars(select(LedgerEntry).where(LedgerEntry.statement_id == statement_id)))


def _all_adjudications(db: Session, statement_id: int):
    return list(db.scalars(select(Adjudication)
                           .where(Adjudication.statement_id == statement_id)
                           .order_by(Adjudication.id)))


def _active_adjudications(db: Session, statement_id: int):
    return [a for a in _all_adjudications(db, statement_id) if a.status == 'active']


# ---------------------------------------------------------------- snapshot
def _refresh(db: Session, statement: ReconciliationStatement) -> dict:
    """重算对账快照，并把失效裁定落库。返回可序列化快照。"""
    now = time.time()
    entries = _entries(db, statement.id)
    submissions = _submissions(db, statement.id)
    every_adjudication = _all_adjudications(db, statement.id)
    active = [a for a in every_adjudication if a.status == 'active']
    by_batch = {}
    latest_submission = {}
    for entry in entries:
        by_batch.setdefault(entry.batch, {})[entry.side] = entry
    for submission in submissions:
        current = latest_submission.get(submission.side)
        if current is None or RevisionKey(submission.revision) > RevisionKey(current.revision):
            latest_submission[submission.side] = submission
    adjudications_by_batch = {}
    for adjudication in every_adjudication:
        adjudications_by_batch.setdefault(adjudication.batch, []).append(adjudication)

    present = {side for side in SIDES if latest_submission.get(side)}
    both_present = present == set(SIDES)
    batches, blockers = [], []
    counts = {'matched': 0, 'resolved': 0, 'disputed': 0, 'missing': 0}
    stale_rows, omitted = [], []
    newest = {side: (latest_submission[side].revision if latest_submission.get(side) else None)
              for side in SIDES}
    for batch in sorted(by_batch, key=_batch_key):
        sides = by_batch[batch]
        ledger, inspection = sides.get('ledger'), sides.get('inspection')
        row = {'batch': batch,
               'line': (ledger or inspection).line,
               'ledger': _entry_dict(ledger, newest['ledger']),
               'inspection': _entry_dict(inspection, newest['inspection']),
               'status': 'missing', 'missing_side': None, 'difference': None,
               'chosen': None, 'stale_adjudication': None}
        if ledger is None or inspection is None:
            row['missing_side'] = 'ledger' if ledger is None else 'inspection'
            counts['missing'] += 1
            history = adjudications_by_batch.get(batch, [])
            for adjudication in history:
                if adjudication.status == 'active':
                    adjudication.status = 'stale'
                    adjudication.stale_at = now
                    adjudication.stale_reason = '该窑次已从源表缺失，原裁定失效'
            if history and history[-1].status == 'stale':
                latest = history[-1]
                row['stale_adjudication'] = {
                    'id': latest.id, 'reason': latest.stale_reason,
                    'side': latest.side, 'side_label': SIDE_LABELS[latest.side],
                    'fired': latest.fired, 'passed': latest.passed,
                    'decided_by': latest.decided_by, 'decided_at': latest.decided_at}
            if both_present:
                blockers.append(_bad('missing',
                                     f'窑次 {batch} 缺少{SIDE_LABELS[row["missing_side"]]}记录', batch))
        elif ledger.fired == inspection.fired and ledger.passed == inspection.passed:
            row['status'] = 'matched'
            counts['matched'] += 1
            for adjudication in adjudications_by_batch.get(batch, []):
                if adjudication.status == 'active':
                    adjudication.status = 'stale'
                    adjudication.stale_at = now
                    adjudication.stale_reason = '两侧件数已一致，原裁定不再需要'
        else:
            row['difference'] = {'fired': inspection.fired - ledger.fired,
                                 'passed': inspection.passed - ledger.passed}
            chosen = None
            history = adjudications_by_batch.get(batch, [])
            for adjudication in history:
                if adjudication.status != 'active' or not _evidence_matches(
                        adjudication.evidence, ledger, inspection):
                    if adjudication.status == 'active':
                        adjudication.status = 'stale'
                        adjudication.stale_at = now
                        adjudication.stale_reason = '源数据或修订在裁定后发生变化'
                    continue
                if chosen is None:
                    chosen = adjudication
            if chosen is None and history:
                latest = history[-1]
                if latest.status == 'stale':
                    row['stale_adjudication'] = {'id': latest.id,
                                                 'reason': latest.stale_reason,
                                                 'side': latest.side,
                                                 'side_label': SIDE_LABELS[latest.side],
                                                 'fired': latest.fired, 'passed': latest.passed,
                                                 'decided_by': latest.decided_by,
                                                 'decided_at': latest.decided_at,
                                                 'stale_at': latest.stale_at}
            if chosen is not None:
                row['status'] = 'resolved'
                row['chosen'] = {'side': chosen.side, 'side_label': SIDE_LABELS[chosen.side],
                                 'fired': chosen.fired, 'passed': chosen.passed,
                                 'reason': chosen.reason, 'adjudication_id': chosen.id,
                                 'decided_by': chosen.decided_by, 'decided_at': chosen.decided_at}
                counts['resolved'] += 1
            else:
                row['status'] = 'disputed'
                counts['disputed'] += 1
                if row['stale_adjudication']:
                    blockers.append(_bad('stale_adjudication',
                                         f'窑次 {batch} 的裁定因源数据更新失效，请重新裁定', batch))
                else:
                    blockers.append(_bad('difference',
                                         f'窑次 {batch} 两侧件数不符，待裁定', batch))
        row['eligible'] = row['status'] in ('matched', 'resolved')
        if row['status'] == 'missing':
            omitted.append({'batch': batch, 'reason': 'missing',
                            'message': f'缺少{SIDE_LABELS[row["missing_side"]]}记录'})
        elif row['status'] == 'disputed':
            omitted.append({'batch': batch, 'reason': 'disputed',
                            'message': '两侧件数不符且尚无有效裁定'})
        if row['status'] == 'disputed' and row['stale_adjudication']:
            stale_rows.append({'batch': batch, **row['stale_adjudication']})
        batches.append(row)

    if not both_present:
        state = 'awaiting_sources'
        missing_sides = [_bad('side_missing', f'尚未收到{SIDE_LABELS[side]}')
                         for side in SIDES if side not in present]
        blockers = missing_sides + blockers
    elif counts['missing']:
        state = 'missing'
    elif counts['disputed']:
        state = 'needs_adjudication'
    elif sum(1 for row in batches if row['eligible']) < 5:
        state = 'ready'
        blockers.append(_bad('insufficient',
                             '已对齐的窑次不足 5 个，发布分析批次至少需要 5 个'))
    else:
        state = 'ready'
    db.flush()
    active = _active_adjudications(db, statement.id)

    sides = {}
    for side in SIDES:
        submission = latest_submission.get(side)
        row_count = sum(1 for e in entries if e.side == side)
        sides[side] = ({'revision': submission.revision, 'source_name': submission.source_name,
                        'submitted_at': submission.created_at, 'submission_id': submission.id,
                        'rows': row_count} if submission else None)

    carried = [{'batch': row['batch'], 'side': side, 'revision': row[side]['revision'],
                'latest_revision': newest[side]}
               for row in batches for side in SIDES
               if row.get(side) and row[side]['carried_forward']]
    token = _fingerprint(statement.id, sides, entries, active)
    snapshot = {'statement_id': statement.id, 'name': statement.name, 'state': state,
                'can_publish': state == 'ready' and not blockers, 'blockers': blockers,
                'sides': sides, 'totals': {'ledger': sum(1 for e in entries if e.side == 'ledger'),
                                           'inspection': sum(1 for e in entries if e.side == 'inspection'),
                                           **counts},
                'batches': batches, 'omitted': omitted, 'stale_adjudications': stale_rows,
                'carried_forward': carried,
                'eligible_batches': sum(1 for b in batches if b['eligible']),
                'revision_token': token, 'generated_at': now}
    statement.state = state
    statement.snapshot = snapshot
    statement.ledger_revision = sides['ledger']['revision'] if sides['ledger'] else ''
    statement.inspection_revision = sides['inspection']['revision'] if sides['inspection'] else ''
    statement.updated_at = now
    db.flush()
    return snapshot


def _fingerprint(statement_id, sides, entries, active):
    """内容令牌：只由两侧修订、现行取值和有效裁定决定。

    同一内容无论重试多少次都是同一令牌，发布因此幂等；裁定记录的数据库 id
    不参与计算，避免“内容没变却重开版本”。
    """
    payload = {
        'statement': statement_id,
        'sides': {side: (sides[side] or {}).get('revision') for side in SIDES},
        'entries': sorted([(e.side, e.batch, e.fired, e.passed, e.revision) for e in entries]),
        'adjudications': sorted((a.batch, a.side, a.fired, a.passed) for a in active),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def summary(snap: dict) -> dict:
    """对账快照的轻量摘要：列表与写操作返回，避免重复传输逐窑明细。"""
    return {'statement_id': snap['statement_id'], 'name': snap['name'], 'state': snap['state'],
            'can_publish': snap['can_publish'], 'blockers': snap['blockers'],
            'sides': snap['sides'], 'totals': snap['totals'],
            'eligible_batches': snap['eligible_batches'], 'omitted': snap['omitted'],
            'stale_adjudications': snap['stale_adjudications'],
            'carried_forward': snap['carried_forward'],
            'revision_token': snap['revision_token'], 'generated_at': snap['generated_at']}


def snapshot(db: Session, statement_id: int) -> dict:
    statement = _locked_statement(db, statement_id)
    return _refresh(db, statement)


def statement_dict(statement: ReconciliationStatement) -> dict:
    snap = statement.snapshot or {}
    return {'id': statement.id, 'name': statement.name, 'state': statement.state,
            'ledger_revision': statement.ledger_revision,
            'inspection_revision': statement.inspection_revision,
            'published_version': statement.published_version,
            'created_by': statement.created_by,
            'created_at': statement.created_at, 'updated_at': statement.updated_at,
            'totals': snap.get('totals', {}), 'can_publish': snap.get('can_publish', False),
            'blocker_count': len(snap.get('blockers', []))}


# ---------------------------------------------------------------- ingest
def ingest(db: Session, statement_id: int, payload, user):
    statement = _locked_statement(db, statement_id)
    side = getattr(payload, 'side', None)
    if side not in SIDES:
        raise HTTPException(422, '来源必须是出窑台账（ledger）或终检表（inspection）')
    raw_revision = str(payload.revision or '').strip()
    if len(raw_revision) > 80:
        raise HTTPException(422, '修订号不能超过 80 个字符')
    rows, revision = _normalize_records(payload.records, raw_revision)
    rows_checksum = checksum({'side': side, 'revision': revision, 'records': rows})

    existing_same_revision = db.scalar(select(SourceSubmission).where(
        SourceSubmission.statement_id == statement.id, SourceSubmission.side == side,
        SourceSubmission.revision == revision))
    if existing_same_revision is not None:
        if existing_same_revision.checksum == rows_checksum:
            _refresh(db, statement)
            return {'submission': _submission_dict(existing_same_revision), 'replayed': True,
                    'applied': existing_same_revision.applied, 'ignored': existing_same_revision.ignored,
                    'snapshot': summary(statement.snapshot)}
        raise HTTPException(409, f'{SIDE_LABELS[side]}修订 {revision} 已导入且内容不同，请提升修订号')

    entries = {e.batch: e for e in _entries(db, statement.id) if e.side == side}
    floor = None
    for previous in _submissions(db, statement.id):
        if previous.side == side and (floor is None or
                                      RevisionKey(previous.revision) > RevisionKey(floor)):
            floor = previous.revision
    plan = []
    for row in rows:
        current = entries.get(row['batch'])
        # 整表快照语义：比该侧现行修订更旧的提交整体让位，连它带来的“新窑次”也不复活
        if floor is not None and compare(revision, floor) < 0:
            plan.append((row, 'ignore', f'迟到旧修订，现行修订 {floor} 已生效'))
            continue
        if current is None:
            plan.append((row, 'apply', '新窑次'))
            continue
        relation = compare(revision, current.revision)
        if relation > 0:
            plan.append((row, 'apply', f'替代修订 {current.revision}'))
        elif relation == 0:
            if (current.fired, current.passed, current.line) != (row['fired'], row['passed'], row['line']):
                raise HTTPException(409, f'窑次 {row["batch"]} 在修订 {revision} 下取值冲突，请提升修订号')
            plan.append((row, 'noop', '同修订同取值'))
        else:
            plan.append((row, 'ignore', f'迟到旧修订，现行修订 {current.revision}'))

    submitted = {row['batch'] for row in rows}
    retracted = []
    if floor is None or compare(revision, floor) >= 0:
        # 整表快照：更新后仍未出现的窑次视为该侧撤回，不能让旧值继续生效
        retracted = sorted((batch for batch, entry in entries.items()
                            if batch not in submitted and compare(revision, entry.revision) > 0),
                           key=_batch_key)

    now = time.time()
    submission = SourceSubmission(statement_id=statement.id, side=side, revision=revision,
                                  source_name=str(payload.source_name or '')[:200],
                                  records=rows, checksum=rows_checksum,
                                  applied=[r['batch'] for r, action, _ in plan if action == 'apply'],
                                  ignored=[r['batch'] for r, action, _ in plan if action == 'ignore'],
                                  retracted=retracted,
                                  submitted_by=user.email, created_at=now)
    db.add(submission)
    db.flush()

    changed, stale = [], []
    for row, action, _ in plan:
        if action != 'apply':
            continue
        entry = entries.get(row['batch'])
        if entry is None:
            db.add(LedgerEntry(statement_id=statement.id, side=side, batch=row['batch'],
                               line=row['line'], fired=row['fired'], passed=row['passed'],
                               revision=revision, submission_id=submission.id, received_at=now))
        else:
            entry.line, entry.fired, entry.passed = row['line'], row['fired'], row['passed']
            entry.revision = revision
            entry.submission_id, entry.received_at = submission.id, now
        changed.append(row['batch'])
    for batch in retracted:
        db.delete(entries[batch])
        changed.append(batch)
    if changed:
        # 源数据更新使该窑的差异裁定失效：必须按新修订重新裁定后才能发布
        for adjudication in db.scalars(select(Adjudication).where(
                Adjudication.statement_id == statement.id, Adjudication.status == 'active',
                Adjudication.batch.in_(changed))):
            adjudication.status = 'stale'
            adjudication.stale_at = now
            adjudication.stale_reason = (
                f'{SIDE_LABELS[side]}更新至修订 {revision}，原裁定失效，请按新值重新裁定')
            stale.append(adjudication.batch)
    db.flush()

    snap = _refresh(db, statement)
    return {'submission': _submission_dict(submission), 'replayed': False,
            'applied': submission.applied, 'ignored': submission.ignored,
            'changed_batches': changed, 'retracted': retracted,
            'invalidated_adjudications': sorted(set(stale)),
            'snapshot': summary(snap)}


def _submission_dict(submission: SourceSubmission) -> dict:
    return {'id': submission.id, 'side': submission.side, 'side_label': SIDE_LABELS[submission.side],
            'revision': submission.revision, 'source_name': submission.source_name,
            'rows': len(submission.records), 'checksum': submission.checksum,
            'applied': submission.applied, 'ignored': submission.ignored,
            'retracted': submission.retracted,
            'submitted_by': submission.submitted_by, 'created_at': submission.created_at}


# ---------------------------------------------------------------- adjudication
def adjudicate(db: Session, statement_id: int, payload, user):
    statement = _locked_statement(db, statement_id)
    snap = _refresh(db, statement)
    token = getattr(payload, 'expected_token', None)
    if token and token != snap['revision_token']:
        raise HTTPException(409, '对账快照已变化（源数据或旧裁定已更新），请刷新后重新裁定')
    batch = str(payload.batch or '').strip()
    row = next((b for b in snap['batches'] if b['batch'] == batch), None)
    if row is None:
        raise HTTPException(404, '对账单中没有该窑次')
    if row['status'] == 'matched':
        raise HTTPException(409, '两侧件数一致，无需裁定')
    if row['status'] == 'missing':
        raise HTTPException(409, f'缺少{SIDE_LABELS[row["missing_side"]]}记录，请先补齐源数据再处理差异')
    side = payload.side
    if side not in SIDES:
        raise HTTPException(422, '裁定取值必须来自出窑台账或终检表')
    reason = str(payload.reason or '').strip()
    if len(reason) < 4:
        raise HTTPException(422, '请记录裁定理由（至少 4 个字符）')
    if len(reason) > 1000:
        raise HTTPException(422, '裁定理由不能超过 1000 个字符')
    source = row[side]
    if source is None:
        raise HTTPException(422, '所选来源没有该窑次记录')
    now = time.time()
    entries = {e.side: e for e in _entries(db, statement.id) if e.batch == batch}
    for previous in db.scalars(select(Adjudication).where(
            Adjudication.statement_id == statement.id, Adjudication.batch == batch,
            Adjudication.status == 'active')):
        previous.status = 'stale'
        previous.stale_at = now
        previous.stale_reason = '被新的裁定替代'
    adjudication = Adjudication(statement_id=statement.id, batch=batch, side=side,
                                line=source['line'], fired=source['fired'], passed=source['passed'],
                                reason=reason,
                                evidence=_evidence(entries.get('ledger'),
                                                   entries.get('inspection')),
                                status='active', decided_by=user.email, decided_at=now)
    db.add(adjudication)
    db.flush()
    snap = _refresh(db, statement)
    return {'adjudication': _adjudication_dict(adjudication), 'snapshot': summary(snap)}


def _adjudication_dict(adjudication: Adjudication) -> dict:
    return {'id': adjudication.id, 'batch': adjudication.batch, 'side': adjudication.side,
            'side_label': SIDE_LABELS[adjudication.side], 'fired': adjudication.fired,
            'passed': adjudication.passed, 'reason': adjudication.reason,
            'evidence': adjudication.evidence, 'status': adjudication.status,
            'decided_by': adjudication.decided_by, 'decided_at': adjudication.decided_at,
            'stale_reason': adjudication.stale_reason, 'stale_at': adjudication.stale_at}


# ---------------------------------------------------------------- publish
def _next_version(db: Session, statement_id: int) -> int:
    versions = list(db.scalars(select(PublishedBatch.version)
                               .where(PublishedBatch.statement_id == statement_id)))
    return max(versions, default=0) + 1


def _published_dict(published: PublishedBatch) -> dict:
    return {'id': published.id, 'statement_id': published.statement_id,
            'version': published.version, 'request_id': published.request_id,
            'fingerprint': published.fingerprint, 'dataset_id': published.dataset_id,
            'job_id': published.job_id, 'ledger_revision': published.ledger_revision,
            'inspection_revision': published.inspection_revision,
            'batch_count': published.batch_count, 'adjudications': published.adjudications,
            'omitted': published.omitted,
            'published_by': published.published_by, 'published_at': published.published_at}


def publish(db: Session, statement_id: int, payload, user):
    statement = _locked_statement(db, statement_id)
    request_id = str(payload.request_id or '').strip()
    if len(request_id) < 8:
        raise HTTPException(422, '发布请求需携带至少 8 位的 request_id，以便失败重试不重复建批次')
    token = getattr(payload, 'expected_token', None)
    snap = _refresh(db, statement)
    # 幂等键先于新鲜度校验：响应丢失后的重试能拿回同一批次，即使期间源数据已更新
    existing = db.scalar(select(PublishedBatch).where(
        PublishedBatch.statement_id == statement.id, PublishedBatch.request_id == request_id))
    if existing is not None:
        return {**_published_dict(existing), 'created': False, 'snapshot': summary(snap)}
    if token and token != snap['revision_token']:
        raise HTTPException(409, '预览后源数据或裁定已变化，请刷新对账单后重新发布')
    existing = db.scalar(select(PublishedBatch).where(
        PublishedBatch.statement_id == statement.id,
        PublishedBatch.fingerprint == snap['revision_token']))
    if existing is not None:
        return {**_published_dict(existing), 'created': False, 'snapshot': summary(snap)}

    if not snap['can_publish']:
        lead = snap['blockers'][0]['message'] if snap['blockers'] else '双方未到齐'
        more = f'（共 {len(snap["blockers"])} 项待处理）' if len(snap['blockers']) > 1 else ''
        raise HTTPException(409, f'对账单尚未就绪，不能发布分析批次：{lead}{more}')

    skipped = [row['batch'] for row in snap['batches'] if not row.get('eligible')]
    if skipped:
        raise HTTPException(409, f'仍有 {len(skipped)} 个窑次未对齐，不能发布：{skipped[0]} 等')

    version = _next_version(db, statement.id)
    records = []
    adjudications = {}
    for row in snap['batches']:
        if row['status'] == 'matched':
            side, source = 'matched', row['ledger']
        else:
            side, source = row['chosen']['side'], row[row['chosen']['side']]
            adjudications[row['batch']] = {**row['chosen'], 'ledger': row['ledger'],
                                           'inspection': row['inspection']}
        records.append({'batch': row['batch'], 'line': source['line'],
                        'fired': source['fired'], 'passed': source['passed'],
                        'source': side,
                        'ledger_revision': row['ledger']['revision'],
                        'inspection_revision': row['inspection']['revision']})
    dataset_name = f'{statement.name} · 对账批次 v{version}'
    source_label = (f'{statement.name} · ledger@{snap["sides"]["ledger"]["revision"]}'
                    f' / inspection@{snap["sides"]["inspection"]["revision"]}')
    dataset = create_dataset(db, DatasetInput(name=dataset_name, source=source_label,
                                             records=records))
    params = {p['key']: p['default'] for p in metadata().get('parameters', [])}
    job = Job(name=f'{statement.name} · v{version} 分布复盘', dataset_id=dataset.id,
              params=params, status='queued', created_by=user.id, created_at=time.time())
    db.add(job)
    db.flush()
    published = PublishedBatch(statement_id=statement.id, version=version, request_id=request_id,
                               fingerprint=snap['revision_token'], dataset_id=dataset.id,
                               job_id=job.id, ledger_revision=snap['sides']['ledger']['revision'],
                               inspection_revision=snap['sides']['inspection']['revision'],
                               adjudications=adjudications, records=records,
                               batch_count=len(records), omitted=snap['omitted'],
                               published_by=user.email,
                               published_at=time.time())
    db.add(published)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        replay = db.scalar(select(PublishedBatch).where(
            PublishedBatch.statement_id == statement_id,
            (PublishedBatch.request_id == request_id) |
            (PublishedBatch.fingerprint == snap['revision_token'])))
        if replay is not None:
            fresh = _locked_statement(db, statement_id)
            return {**_published_dict(replay), 'created': False,
                    'snapshot': summary(_refresh(db, fresh))}
        raise HTTPException(409, '发布发生并发冲突，请刷新后重试')
    statement.published_version = version
    statement.updated_at = time.time()
    db.flush()
    return {**_published_dict(published), 'created': True, 'snapshot': summary(snap)}


# ---------------------------------------------------------------- trace
def trace(db: Session, statement_id: int, batch: str) -> dict:
    statement = _locked_statement(db, statement_id)
    snap = _refresh(db, statement)
    row = next((b for b in snap['batches'] if b['batch'] == batch), None)
    if row is None:
        raise HTTPException(404, '对账单中没有该窑次')
    submissions = []
    for submission in _submissions(db, statement.id):
        hit = next((r for r in submission.records if r['batch'] == batch), None)
        if hit is None:
            continue
        outcome = ('applied' if batch in submission.applied else
                   'ignored' if batch in submission.ignored else 'unchanged')
        submissions.append({'submission_id': submission.id, 'side': submission.side,
                            'side_label': SIDE_LABELS[submission.side], 'revision': submission.revision,
                            'source_name': submission.source_name, 'received_at': submission.created_at,
                            'fired': hit['fired'], 'passed': hit['passed'], 'line': hit['line'],
                            'outcome': outcome, 'submitted_by': submission.submitted_by})
    adjudications = [_adjudication_dict(a) for a in db.scalars(select(Adjudication).where(
        Adjudication.statement_id == statement.id, Adjudication.batch == batch)
        .order_by(Adjudication.decided_at, Adjudication.id))]
    publications = []
    for published in db.scalars(select(PublishedBatch).where(
            PublishedBatch.statement_id == statement.id).order_by(PublishedBatch.version)):
        record = next((r for r in published.records if r['batch'] == batch), None)
        if record is not None:
            publications.append({'id': published.id, 'version': published.version,
                                 'published_at': published.published_at,
                                 'ledger_revision': published.ledger_revision,
                                 'inspection_revision': published.inspection_revision,
                                 'record': record})
    return {'batch': batch, 'current': {'ledger': row['ledger'], 'inspection': row['inspection'],
                                        'status': row['status'], 'chosen': row['chosen']},
            'submissions': submissions, 'adjudications': adjudications,
            'publications': publications}


def publications(db: Session, statement_id: int) -> list[dict]:
    statement = _locked_statement(db, statement_id)
    return [_published_dict(p) for p in db.scalars(select(PublishedBatch)
                                                   .where(PublishedBatch.statement_id == statement.id)
                                                   .order_by(PublishedBatch.version.desc()))]
