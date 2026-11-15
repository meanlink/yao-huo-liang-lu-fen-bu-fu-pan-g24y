from pydantic import BaseModel, Field


class StatementInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)


class IngestInput(BaseModel):
    side: str = Field(description='ledger=出窑台账，inspection=终检表')
    revision: str = Field(default='', max_length=80)
    source_name: str = Field(default='', max_length=200)
    records: list[dict] = Field(min_length=1, max_length=5000)


class AdjudicationInput(BaseModel):
    batch: str = Field(min_length=1, max_length=120)
    side: str
    reason: str = Field(min_length=4, max_length=1000)
    expected_token: str = Field(min_length=8, max_length=128,
                                 description='裁定前看到的对账快照令牌')


class PublishInput(BaseModel):
    request_id: str = Field(min_length=8, max_length=80)
    expected_token: str = Field(min_length=8, max_length=128,
                                description='发布前看到的对账快照令牌')
