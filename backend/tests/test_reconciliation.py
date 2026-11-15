"""双来源对账的接口级测试：到齐、差异裁定、修订择优、发布幂等与版本化。

运行：在 backend/ 目录执行 `python -m pytest tests/test_reconciliation.py -q`
（测试使用临时 SQLite 与临时产物目录，不连接 Docker 服务）。
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix='recon-tests-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TMP, 'test.db')
os.environ['ARTIFACT_DIR'] = os.path.join(_TMP, 'artifacts')
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6399/0'
os.environ['SEED_DEMO'] = 'false'

from fastapi.testclient import TestClient  # noqa: E402

from migrations.initial import migrate  # noqa: E402
from scripts.seed_demo import seed  # noqa: E402
from src.config.database import Session  # noqa: E402
from src.main import app  # noqa: E402
from src.models import Adjudication, Dataset, Job, PublishedBatch, SourceSubmission  # noqa: E402
from src.utils.revisions import RevisionKey, is_newer  # noqa: E402

LEDGER = [
    {'batch': 'QH-001', 'line': '东窑', 'fired': 200, 'passed': 190},
    {'batch': 'QH-002', 'line': '中窑', 'fired': 210, 'passed': 195},
    {'batch': 'QH-003', 'line': '西窑', 'fired': 220, 'passed': 213},
    {'batch': 'QH-004', 'line': '东窑', 'fired': 230, 'passed': 200},
    {'batch': 'QH-005', 'line': '中窑', 'fired': 240, 'passed': 230},
    {'batch': 'QH-006', 'line': '西窑', 'fired': 205, 'passed': 190},
]
INSPECTION = [dict(row) for row in LEDGER]
INSPECTION[3] = {'batch': 'QH-004', 'line': '东窑', 'fired': 230, 'passed': 208}


@pytest.fixture(scope='session', autouse=True)
def prepared():
    migrate()
    seed()


def client(email='admin@demo.local'):
    c = TestClient(app)
    response = c.post('/auth/login', json={'email': email, 'password': 'Demo123!'})
    assert response.status_code == 200, response.text
    c.headers['Authorization'] = 'Bearer ' + response.json()['access_token']
    return c


@pytest.fixture()
def statement():
    admin = client()
    name = 'pytest 对账 ' + next(tempfile._get_candidate_names())
    created = admin.post('/reconciliations', json={'name': name})
    assert created.status_code == 201, created.text
    return admin, created.json()['id']


def ingest(admin, sid, side, revision, records, source='源表.csv'):
    return admin.post(f'/reconciliations/{sid}/sources',
                      json={'side': side, 'revision': revision,
                            'source_name': source, 'records': records})


def token(admin, sid):
    response = admin.get(f'/reconciliations/{sid}')
    assert response.status_code == 200, response.text
    return response.json()['revision_token']


def test_revision_ordering():
    assert is_newer('10', '9')
    assert not is_newer('9', '10')
    assert is_newer('1.2.1', '1.2')
    assert is_newer('R-0042', 'R-0041')
    assert RevisionKey('3.0') == RevisionKey('3')
    assert not is_newer('2.5', '3')


def test_needs_both_sources_before_publish(statement):
    admin, sid = statement
    assert ingest(admin, sid, 'ledger', '3', LEDGER).status_code == 201
    snap = admin.get(f'/reconciliations/{sid}').json()
    assert snap['state'] == 'awaiting_sources' and snap['can_publish'] is False
    assert any(item['code'] == 'side_missing' for item in snap['blockers'])
    blocked = admin.post(f'/reconciliations/{sid}/publish',
                         json={'request_id': 'req-both-0001', 'expected_token': snap['revision_token']})
    assert blocked.status_code == 409
    assert '终检表' in blocked.json()['detail']


def test_difference_blocks_until_adjudicated(statement):
    admin, sid = statement
    ingest(admin, sid, 'ledger', '3', LEDGER)
    body = ingest(admin, sid, 'inspection', '2', INSPECTION).json()
    assert body['snapshot']['state'] == 'needs_adjudication'
    assert body['snapshot']['totals']['disputed'] == 1

    preview = admin.post(f'/reconciliations/{sid}/publish',
                         json={'request_id': 'req-diff-0001',
                               'expected_token': body['snapshot']['revision_token']})
    assert preview.status_code == 409
    assert 'QH-004' in preview.json()['detail']

    decided = admin.post(f'/reconciliations/{sid}/adjudications', json={
        'batch': 'QH-004', 'side': 'inspection', 'expected_token': token(admin, sid),
        'reason': '终检复称，出窑台账少记 8 件'})
    assert decided.status_code == 201, decided.text
    body = admin.get(f'/reconciliations/{sid}').json()
    assert body['state'] == 'ready' and body['can_publish'] is True
    qh4 = next(row for row in body['batches'] if row['batch'] == 'QH-004')
    assert qh4['chosen']['side'] == 'inspection' and qh4['chosen']['passed'] == 208


def test_adjudication_records_value_and_reason(statement):
    admin, sid = statement
    ingest(admin, sid, 'ledger', '3', LEDGER)
    ingest(admin, sid, 'inspection', '2', INSPECTION)
    missing_reason = admin.post(f'/reconciliations/{sid}/adjudications', json={
        'batch': 'QH-004', 'side': 'inspection', 'reason': '短',
        'expected_token': token(admin, sid)})
    assert missing_reason.status_code == 422
    bad_side = admin.post(f'/reconciliations/{sid}/adjudications', json={
        'batch': 'QH-004', 'side': 'other', 'reason': '取值必须来自两侧之一',
        'expected_token': token(admin, sid)})
    assert bad_side.status_code == 422
    ok = admin.post(f'/reconciliations/{sid}/adjudications', json={
        'batch': 'QH-004', 'side': 'inspection', 'reason': '终检复称',
        'expected_token': token(admin, sid)})
    assert ok.status_code == 201
    record = ok.json()['adjudication']
    assert (record['fired'], record['passed'], record['reason']) == (230, 208, '终检复称')
    assert record['side_label'] == '终检表' and record['evidence']['ledger_revision'] == '3'


def test_late_revision_does_not_overwrite(statement):
    admin, sid = statement
    ingest(admin, sid, 'ledger', '3', LEDGER)
    late = ingest(admin, sid, 'ledger', '2.5', [
        {'batch': 'QH-001', 'line': '东窑', 'fired': 200, 'passed': 150}]).json()
    assert late['ignored'] == ['QH-001'] and late['applied'] == []
    qh1 = next(row for row in admin.get(f'/reconciliations/{sid}').json()['batches']
               if row['batch'] == 'QH-001')
    assert qh1['ledger']['passed'] == 190 and qh1['ledger']['revision'] == '3'


def test_late_submission_cannot_introduce_batches(statement):
    admin, sid = statement
    ingest(admin, sid, 'ledger', '3', LEDGER)
    ingest(admin, sid, 'inspection', '2', [row for row in INSPECTION if row['batch'] != 'QH-006'])
    assert ingest(admin, sid, 'inspection', '4', INSPECTION).status_code == 201
    # 迟到的 3 版带着一窑终检表没有的 QH-007；它比现行 4 版旧，整份快照让位
    late_rows = INSPECTION + [{'batch': 'QH-007', 'line': '东窑', 'fired': 201, 'passed': 190}]
    late = ingest(admin, sid, 'inspection', '3', late_rows).json()
    assert late['ignored'] == [row['batch'] for row in late_rows]
    assert late['applied'] == []
    body = admin.get(f'/reconciliations/{sid}').json()
    # 迟到的旧快照既不能复活被删的窑次，也不能凭空带进新窑次
    assert all(row['batch'] != 'QH-007' for row in body['batches'])
    assert body['totals']['inspection'] == 6


def test_missing_side_invalidates_adjudication(statement):
    admin, sid = statement
    ingest(admin, sid, 'ledger', '3', LEDGER)
    ingest(admin, sid, 'inspection', '2', INSPECTION)
    admin.post(f'/reconciliations/{sid}/adjudications', json={
        'batch': 'QH-004', 'side': 'inspection', 'reason': '终检复称',
        'expected_token': token(admin, sid)})
    # 终检表新修订整表重发但漏了一窑：该窑的裁定随之失效
    assert ingest(admin, sid, 'inspection', '3',
                  [row for row in INSPECTION if row['batch'] != 'QH-004']).status_code == 201
    body = admin.get(f'/reconciliations/{sid}').json()
    qh4 = next(row for row in body['batches'] if row['batch'] == 'QH-004')
    assert qh4['status'] == 'missing' and qh4['stale_adjudication']
    with Session() as db:
        assert db.query(Adjudication).filter_by(statement_id=sid, batch='QH-004',
                                                status='stale').count() == 1


def test_same_revision_replay_and_conflict(statement):
    admin, sid = statement
    first = ingest(admin, sid, 'ledger', '3', LEDGER)
    assert first.status_code == 201
    replay = ingest(admin, sid, 'ledger', '3', LEDGER)
    assert replay.status_code == 201 and replay.json()['replayed'] is True
    conflict = ingest(admin, sid, 'ledger', '3', LEDGER[:5])
    assert conflict.status_code == 409
    with Session() as db:
        assert db.query(SourceSubmission).filter_by(statement_id=sid, side='ledger').count() == 1


def test_source_update_invalidates_adjudication(statement):
    admin, sid = statement
    ingest(admin, sid, 'ledger', '3', LEDGER)
    ingest(admin, sid, 'inspection', '2', INSPECTION)
    stale = token(admin, sid)
    admin.post(f'/reconciliations/{sid}/adjudications', json={
        'batch': 'QH-004', 'side': 'inspection', 'reason': '终检复称',
        'expected_token': stale})
    # 源表以更高修订重发，且该窑取值变化 -> 裁定失效
    revised = [dict(row) for row in LEDGER]
    revised[3] = {'batch': 'QH-004', 'line': '东窑', 'fired': 230, 'passed': 205}
    assert ingest(admin, sid, 'ledger', '4', revised).status_code == 201
    body = admin.get(f'/reconciliations/{sid}').json()
    qh4 = next(row for row in body['batches'] if row['batch'] == 'QH-004')
    assert qh4['status'] == 'disputed' and qh4['stale_adjudication']['side'] == 'inspection'
    assert body['stale_adjudications']
    with Session() as db:
        assert db.query(Adjudication).filter_by(statement_id=sid, status='stale').count() == 1
    # 依据失效裁定拿到的旧令牌不能用于发布
    blocked = admin.post(f'/reconciliations/{sid}/publish',
                         json={'request_id': 'req-stale-0001', 'expected_token': stale})
    assert blocked.status_code == 409


def test_publish_is_idempotent_and_versioned(statement):
    admin, sid = statement
    ingest(admin, sid, 'ledger', '3', LEDGER)
    ingest(admin, sid, 'inspection', '2', INSPECTION)
    admin.post(f'/reconciliations/{sid}/adjudications', json={
        'batch': 'QH-004', 'side': 'inspection', 'reason': '终检复称',
        'expected_token': token(admin, sid)})
    first = admin.post(f'/reconciliations/{sid}/publish',
                       json={'request_id': 'req-pub-0001', 'expected_token': token(admin, sid)})
    assert first.status_code == 201, first.text
    published = first.json()
    assert published['created'] is True and published['version'] == 1
    assert published['batch_count'] == 6 and published['adjudications']['QH-004']['side'] == 'inspection'

    retry = admin.post(f'/reconciliations/{sid}/publish',
                       json={'request_id': 'req-pub-0001', 'expected_token': token(admin, sid)})
    assert retry.status_code == 200 and retry.json()['id'] == published['id']
    assert retry.json()['created'] is False
    other = admin.post(f'/reconciliations/{sid}/publish',
                       json={'request_id': 'req-pub-0002', 'expected_token': token(admin, sid)})
    assert other.status_code == 200 and other.json()['id'] == published['id']
    with Session() as db:
        assert db.query(PublishedBatch).filter_by(statement_id=sid).count() == 1
        assert db.query(Dataset).filter_by(name=f'{admin.get(f"/reconciliations/{sid}").json()["name"]} · 对账批次 v1').count() == 1
        job = db.get(Job, published['job_id'])
        assert job.status == 'queued' and len(job.result or {}) == 0

    # 发布后修订：另开 v2，v1 的记录保持原样
    revise = [dict(row) for row in INSPECTION]
    revise[4] = {'batch': 'QH-005', 'line': '中窑', 'fired': 240, 'passed': 229}
    assert ingest(admin, sid, 'inspection', '3', revise).status_code == 201
    body = admin.get(f'/reconciliations/{sid}').json()
    qh4 = next(row for row in body['batches'] if row['batch'] == 'QH-004')
    # 终检表升版重发了 QH-004：该窑的源修订已更新，旧裁定必须重做
    assert qh4['status'] == 'disputed' and qh4['stale_adjudication']
    assert qh4['inspection']['revision'] == '3'
    blocked = admin.post(f'/reconciliations/{sid}/publish',
                         json={'request_id': 'req-pub-0003', 'expected_token': token(admin, sid)})
    assert blocked.status_code == 409
    admin.post(f'/reconciliations/{sid}/adjudications', json={
        'batch': 'QH-004', 'side': 'inspection', 'reason': '终检表升版复称，仍取 208',
        'expected_token': token(admin, sid)})
    admin.post(f'/reconciliations/{sid}/adjudications', json={
        'batch': 'QH-005', 'side': 'inspection', 'reason': '终检复核少计 1 件',
        'expected_token': token(admin, sid)})
    second = admin.post(f'/reconciliations/{sid}/publish',
                        json={'request_id': 'req-pub-0004', 'expected_token': token(admin, sid)})
    assert second.status_code == 201, second.text
    assert second.json()['version'] == 2 and second.json()['dataset_id'] != published['dataset_id']
    with Session() as db:
        old = db.query(PublishedBatch).filter_by(statement_id=sid, version=1).one()
        assert old.records[3] == {**old.records[3], 'batch': 'QH-004', 'passed': 208, 'source': 'inspection'}

    # 响应丢失后的重试即便源数据已更新，也拿回同一批次而不是再建一批
    lost = admin.post(f'/reconciliations/{sid}/publish',
                      json={'request_id': 'req-pub-0001', 'expected_token': 'stale-token-value'})
    assert lost.status_code == 200 and lost.json()['id'] == published['id']
    assert lost.json()['created'] is False


def test_missing_batch_blocks_and_cannot_be_adjudicated(statement):
    admin, sid = statement
    ingest(admin, sid, 'ledger', '3', LEDGER)
    ingest(admin, sid, 'inspection', '2', INSPECTION)
    extra = LEDGER + [{'batch': 'QH-007', 'line': '东窑', 'fired': 201, 'passed': 190}]
    ingest(admin, sid, 'ledger', '4', extra)
    admin.post(f'/reconciliations/{sid}/adjudications', json={
        'batch': 'QH-004', 'side': 'inspection', 'reason': '终检复称',
        'expected_token': token(admin, sid)})
    body = admin.get(f'/reconciliations/{sid}').json()
    assert body['state'] == 'missing' and body['can_publish'] is False
    assert {'batch': 'QH-007', 'reason': 'missing', 'message': '缺少终检表记录'} in body['omitted']
    assert body['eligible_batches'] == 6  # 5 窑一致 + QH-004 已裁定；QH-007 缺失不计入
    refused = admin.post(f'/reconciliations/{sid}/adjudications', json={
        'batch': 'QH-007', 'side': 'ledger', 'reason': '缺失不能以裁定补齐',
        'expected_token': token(admin, sid)})
    assert refused.status_code == 409
    blocked = admin.post(f'/reconciliations/{sid}/publish',
                         json={'request_id': 'req-missing-1', 'expected_token': token(admin, sid)})
    assert blocked.status_code == 409 and 'QH-007' in blocked.json()['detail']


def test_roles_and_trace(statement):
    admin, sid = statement
    ingest(admin, sid, 'ledger', '3', LEDGER)
    ingest(admin, sid, 'inspection', '2', INSPECTION)
    admin.post(f'/reconciliations/{sid}/adjudications', json={
        'batch': 'QH-004', 'side': 'inspection', 'reason': '终检复称',
        'expected_token': token(admin, sid)})
    admin.post(f'/reconciliations/{sid}/publish',
               json={'request_id': 'req-trace-01', 'expected_token': token(admin, sid)})

    analyst = client('analyst@demo.local')
    assert analyst.post(f'/reconciliations/{sid}/adjudications', json={
        'batch': 'QH-004', 'side': 'inspection', 'reason': '分析员不应能裁定',
        'expected_token': token(admin, sid)}).status_code == 403
    assert analyst.post(f'/reconciliations/{sid}/publish', json={
        'request_id': 'req-trace-02', 'expected_token': token(admin, sid)}).status_code == 403
    assert analyst.post(f'/reconciliations/{sid}/sources', json={
        'side': 'ledger', 'revision': '9', 'records': LEDGER}).status_code == 201

    viewer = client('viewer@demo.local')
    assert viewer.post(f'/reconciliations/{sid}/sources', json={
        'side': 'ledger', 'revision': '10', 'records': LEDGER}).status_code == 403
    assert viewer.get(f'/reconciliations/{sid}/trace', params={'batch': 'QH-004'}).status_code == 200

    trace = admin.get(f'/reconciliations/{sid}/trace', params={'batch': 'QH-004'}).json()
    outcomes = [(item['side'], item['revision'], item['outcome']) for item in trace['submissions']]
    assert ('inspection', '2', 'applied') in outcomes
    assert [pub['version'] for pub in trace['publications']] == [1]
    assert trace['publications'][0]['record']['source'] == 'inspection'


def test_publish_requires_request_id_and_valid_records(statement):
    admin, sid = statement
    assert admin.post(f'/reconciliations/{sid}/publish', json={
        'request_id': 'short', 'expected_token': 'x' * 8}).status_code == 422
    bad = ingest(admin, sid, 'ledger', '3', [
        {'batch': 'QH-001', 'line': '东窑', 'fired': 100, 'passed': 120}])
    assert bad.status_code == 422
    negative = ingest(admin, sid, 'ledger', '3', [
        {'batch': 'QH-001', 'line': '东窑', 'fired': -1, 'passed': 0}])
    assert negative.status_code == 422
    duplicate = ingest(admin, sid, 'ledger', '3', [
        {'batch': 'QH-001', 'line': '东窑', 'fired': 100, 'passed': 90},
        {'batch': 'QH-001', 'line': '东窑', 'fired': 100, 'passed': 90}])
    assert duplicate.status_code == 422
    no_revision = ingest(admin, sid, 'ledger', '', LEDGER)
    assert no_revision.status_code == 422


def test_unresolved_statement_not_usable_for_analysis(statement):
    admin, sid = statement
    ingest(admin, sid, 'ledger', '3', LEDGER)
    ingest(admin, sid, 'inspection', '2', INSPECTION)
    with Session() as db:
        before = db.query(Dataset).count()
    assert admin.post(f'/reconciliations/{sid}/publish', json={
        'request_id': 'req-unresolved', 'expected_token': token(admin, sid)}).status_code == 409
    with Session() as db:
        assert db.query(Dataset).count() == before
