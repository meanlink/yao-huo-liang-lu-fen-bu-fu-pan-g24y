import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, ReconciliationStatement
from src.services.business import metadata, seed_records, analyze, seed_source_pairs
from src.services.reconciliation import create_statement, ingest
from src.services.reports import deliver
from src.utils.security import hash_password
from src.utils.serialization import checksum
RECONCILIATION_NAME='11 月第 1 周 · 出窑台账 vs 终检表'
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        admin=db.scalar(select(User).where(User.role=='admin'))
        if admin and not db.scalar(select(ReconciliationStatement).where(
                ReconciliationStatement.name==RECONCILIATION_NAME)):
            ledger, inspection = seed_source_pairs(d.records)
            statement=create_statement(db, RECONCILIATION_NAME, admin)
            from src.schemas.reconciliation import IngestInput
            ingest(db, statement.id, IngestInput(side='ledger', revision='R-0007',
                   source_name='kiln_ledger_202611_w1.csv', records=ledger), admin)
            ingest(db, statement.id, IngestInput(side='inspection', revision='T-0004',
                   source_name='final_inspection_202611_w1.csv', records=inspection), admin)
            db.add(Audit(actor='seed',action='demo.reconciliation',detail={'statement':statement.id},at=time.time()))
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
