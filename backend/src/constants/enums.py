from enum import Enum
class Role(str, Enum):
    admin = 'admin'
    analyst = 'analyst'
    viewer = 'viewer'
class JobStatus(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'

class SourceSide(str, Enum):
    ledger = 'ledger'
    inspection = 'inspection'

class BatchResolution(str, Enum):
    matched = 'matched'
    resolved = 'resolved'
    disputed = 'disputed'
    stale = 'stale_adjudication'
    missing = 'missing'

class StatementState(str, Enum):
    awaiting_sources = 'awaiting_sources'
    missing = 'missing'
    needs_adjudication = 'needs_adjudication'
    ready = 'ready'

class AdjudicationStatus(str, Enum):
    active = 'active'
    stale = 'stale'
