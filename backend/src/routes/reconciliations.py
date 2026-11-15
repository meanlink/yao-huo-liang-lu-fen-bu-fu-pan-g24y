from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select

from src.config.database import get_db
from src.middlewares.auth import current_user, can_write, admin_only
from src.models import Adjudication, ReconciliationStatement, SourceSubmission
from src.schemas.reconciliation import (AdjudicationInput, IngestInput,
                                        PublishInput, StatementInput)
from src.services import reconciliation
from src.services.audit import record
from src.services.jobs import enqueue

router = APIRouter(prefix='/reconciliations')


@router.get('')
def listing(db=Depends(get_db), user=Depends(current_user)):
    return [reconciliation.statement_dict(s) for s in db.scalars(
        select(ReconciliationStatement).order_by(ReconciliationStatement.id.desc()))]


@router.post('', status_code=201)
def create(data: StatementInput, db=Depends(get_db), user=Depends(can_write)):
    statement = reconciliation.create_statement(db, data.name, user)
    record(db, user.email, 'reconciliation.create', {'id': statement.id, 'name': statement.name})
    db.commit()
    return reconciliation.statement_dict(statement)


@router.get('/{id}')
def detail(id: int, db=Depends(get_db), user=Depends(current_user)):
    statement = db.get(ReconciliationStatement, id)
    if not statement:
        raise HTTPException(404, '对账单不存在')
    snap = reconciliation.snapshot(db, id)
    db.commit()
    return {**reconciliation.statement_dict(statement),
            **{k: snap[k] for k in ('state', 'can_publish', 'blockers', 'sides', 'totals',
                                    'batches', 'omitted', 'eligible_batches', 'carried_forward',
                                    'stale_adjudications', 'revision_token', 'generated_at')}}


@router.post('/{id}/sources', status_code=201)
def ingest(id: int, data: IngestInput, db=Depends(get_db), user=Depends(can_write)):
    result = reconciliation.ingest(db, id, data, user)
    record(db, user.email, 'reconciliation.source_ingest',
           {'id': id, 'side': result['submission']['side'], 'revision': result['submission']['revision'],
            'applied': len(result.get('applied', [])), 'ignored': len(result.get('ignored', [])),
            'replayed': result.get('replayed', False)})
    db.commit()
    return result


@router.get('/{id}/sources')
def sources(id: int, db=Depends(get_db), user=Depends(current_user)):
    statement = db.get(ReconciliationStatement, id)
    if not statement:
        raise HTTPException(404, '对账单不存在')
    rows = []
    for submission in db.scalars(select(SourceSubmission)
                                 .where(SourceSubmission.statement_id == id)
                                 .order_by(SourceSubmission.created_at.desc(),
                                           SourceSubmission.id.desc())):
        rows.append(reconciliation._submission_dict(submission))
    return rows


@router.post('/{id}/adjudications', status_code=201)
def adjudicate(id: int, data: AdjudicationInput, db=Depends(get_db), user=Depends(admin_only)):
    result = reconciliation.adjudicate(db, id, data, user)
    record(db, user.email, 'reconciliation.adjudicate',
           {'id': id, 'batch': data.batch, 'side': data.side,
            'reason': data.reason[:200]})
    db.commit()
    return result


@router.get('/{id}/adjudications')
def adjudications(id: int, db=Depends(get_db), user=Depends(current_user)):
    statement = db.get(ReconciliationStatement, id)
    if not statement:
        raise HTTPException(404, '对账单不存在')
    return [reconciliation._adjudication_dict(a) for a in db.scalars(
        select(Adjudication).where(Adjudication.statement_id == id)
        .order_by(Adjudication.id.desc()))]


@router.post('/{id}/publish', status_code=201)
def publish(id: int, data: PublishInput, response: Response,
            db=Depends(get_db), user=Depends(admin_only)):
    result = reconciliation.publish(db, id, data, user)
    record(db, user.email, 'reconciliation.publish',
           {'id': id, 'version': result['version'], 'dataset': result['dataset_id'],
            'job': result['job_id'], 'request_id': data.request_id,
            'created': result.get('created', False)})
    db.commit()
    if result.get('created') and result.get('job_id'):
        enqueue(result['job_id'])
    if not result.get('created'):
        # 重试命中既有批次：语义是“已存在”，不再新建
        response.status_code = 200
    return result


@router.get('/{id}/publications')
def publications(id: int, db=Depends(get_db), user=Depends(current_user)):
    return reconciliation.publications(db, id)


@router.get('/{id}/trace')
def trace(id: int, batch: str = Query(min_length=1, max_length=120),
          db=Depends(get_db), user=Depends(current_user)):
    result = reconciliation.trace(db, id, batch)
    db.commit()
    return result
