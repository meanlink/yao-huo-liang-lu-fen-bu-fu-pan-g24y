"""双来源对账：出窑台账（ledger）与终检表（inspection）。

设计约束：
- 每次导入是一条不可变的 SourceSubmission，保留源文件修订号与行级快照。
- 同侧同窑次只保留“最新适用修订”的取值；迟到修订不会覆盖已生效的新值。
- 某侧的有效修订一旦变化，该窑旧差异裁定即失效（stale），必须重新裁定后才能发布。
- 发布批次是不可变的 PublishedBatch；重试不重复建批次，发布后的修订另开版本。
"""
from sqlalchemy import String, JSON, Double, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class SourceSubmission(Base):
    __tablename__ = 'source_submissions'
    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey('reconciliation_statements.id'), index=True)
    side: Mapped[str] = mapped_column(String(20), index=True)
    revision: Mapped[str] = mapped_column(String(80))
    source_name: Mapped[str] = mapped_column(String(200), default='')
    records: Mapped[list] = mapped_column(JSON, default=list)
    checksum: Mapped[str] = mapped_column(String(64), default='')
    applied: Mapped[list] = mapped_column(JSON, default=list)
    ignored: Mapped[list] = mapped_column(JSON, default=list)
    retracted: Mapped[list] = mapped_column(JSON, default=list)
    submitted_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)
    __table_args__ = (
        UniqueConstraint('statement_id', 'side', 'revision', name='uq_submission_statement_side_revision'),
        Index('ix_submission_statement_side_time', 'statement_id', 'side', 'created_at'),
    )


class LedgerEntry(Base):
    """每侧每窑次当前生效的取值（按修订号择优合并，可追踪来源提交）。"""
    __tablename__ = 'ledger_entries'
    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey('reconciliation_statements.id'), index=True)
    side: Mapped[str] = mapped_column(String(20), index=True)
    batch: Mapped[str] = mapped_column(String(120), index=True)
    line: Mapped[str] = mapped_column(String(80), default='')
    fired: Mapped[float | None] = mapped_column(Double, nullable=True)
    passed: Mapped[float | None] = mapped_column(Double, nullable=True)
    revision: Mapped[str] = mapped_column(String(80))
    submission_id: Mapped[int] = mapped_column(ForeignKey('source_submissions.id'))
    received_at: Mapped[float] = mapped_column(Double)
    __table_args__ = (UniqueConstraint('statement_id', 'side', 'batch', name='uq_entry_statement_side_batch'),)


class Adjudication(Base):
    __tablename__ = 'adjudications'
    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey('reconciliation_statements.id'), index=True)
    batch: Mapped[str] = mapped_column(String(120), index=True)
    side: Mapped[str] = mapped_column(String(20))
    line: Mapped[str] = mapped_column(String(80), default='')
    fired: Mapped[float] = mapped_column(Double)
    passed: Mapped[float] = mapped_column(Double)
    reason: Mapped[str] = mapped_column(String(1000))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default='active', index=True)
    decided_by: Mapped[str] = mapped_column(String(150))
    decided_at: Mapped[float] = mapped_column(Double)
    stale_reason: Mapped[str] = mapped_column(String(500), default='')
    stale_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class ReconciliationStatement(Base):
    """对账单：两侧是否到齐、逐窑差异、裁定与发布状态。"""
    __tablename__ = 'reconciliation_statements'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    state: Mapped[str] = mapped_column(String(30), default='empty', index=True)
    ledger_revision: Mapped[str] = mapped_column(String(80), default='')
    inspection_revision: Mapped[str] = mapped_column(String(80), default='')
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    published_version: Mapped[int] = mapped_column(default=0)
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class PublishedBatch(Base):
    __tablename__ = 'published_batches'
    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey('reconciliation_statements.id'), index=True)
    version: Mapped[int]
    request_id: Mapped[str] = mapped_column(String(80))
    fingerprint: Mapped[str] = mapped_column(String(64))
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey('datasets.id'), nullable=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey('jobs.id'), nullable=True)
    ledger_revision: Mapped[str] = mapped_column(String(80))
    inspection_revision: Mapped[str] = mapped_column(String(80))
    adjudications: Mapped[dict] = mapped_column(JSON, default=dict)
    records: Mapped[list] = mapped_column(JSON, default=list)
    batch_count: Mapped[int] = mapped_column(default=0)
    omitted: Mapped[list] = mapped_column(JSON, default=list)
    published_by: Mapped[str] = mapped_column(String(150))
    published_at: Mapped[float] = mapped_column(Double)
    __table_args__ = (
        UniqueConstraint('statement_id', 'version', name='uq_published_statement_version'),
        UniqueConstraint('statement_id', 'fingerprint', name='uq_published_statement_fingerprint'),
        UniqueConstraint('statement_id', 'request_id', name='uq_published_statement_request'),
    )
