from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import select

from findmyjob.core.enums import FactKind
from findmyjob.db.models import ProfileFactRecord


class PersonalFactRecordView(BaseModel):
    fact_id: str
    kind: FactKind
    payload: dict = Field(default_factory=dict)
    allowed_for_generation: bool = True
    disallowed: bool = False
    provenance: str = 'user'
    confirmed: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_record(cls, record: ProfileFactRecord) -> 'PersonalFactRecordView':
        return cls(
            fact_id=record.fact_id,
            kind=record.kind,
            payload=dict(record.payload or {}),
            allowed_for_generation=record.allowed_for_generation,
            disallowed=record.disallowed,
            provenance=record.provenance,
            confirmed=record.confirmed,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @property
    def summary(self) -> str:
        payload = self.payload or {}
        for key in ('summary', 'name', 'label', 'value', 'title', 'company', 'school', 'degree', 'email'):
            value = str(payload.get(key) or '').strip()
            if value:
                return value[:120]
        return '-'

    @property
    def onboarding_imported(self) -> bool:
        return self.fact_id.startswith('onboard.personal') or self.provenance.startswith('onboarding:')

    @property
    def review_only(self) -> bool:
        return self.disallowed or not self.allowed_for_generation


def list_personal_facts(runtime, *, kind: FactKind | None = None, onboarding_only: bool = False) -> list[PersonalFactRecordView]:
    with runtime.session_scope() as session:
        stmt = select(ProfileFactRecord).order_by(ProfileFactRecord.kind.asc(), ProfileFactRecord.fact_id.asc())
        if kind is not None:
            stmt = stmt.where(ProfileFactRecord.kind == kind)
        records = [PersonalFactRecordView.from_record(record) for record in session.scalars(stmt).all()]
    if onboarding_only:
        records = [record for record in records if record.onboarding_imported]
    return records


def get_personal_fact(runtime, fact_id: str) -> PersonalFactRecordView:
    with runtime.session_scope() as session:
        record = session.scalar(select(ProfileFactRecord).where(ProfileFactRecord.fact_id == fact_id))
        if record is None:
            raise ValueError(f'Profile fact not found: {fact_id}')
        return PersonalFactRecordView.from_record(record)


def update_personal_fact_flags(runtime, fact_id: str, *, allowed_for_generation: bool | None = None, disallowed: bool | None = None) -> PersonalFactRecordView:
    with runtime.session_scope() as session:
        record = session.scalar(select(ProfileFactRecord).where(ProfileFactRecord.fact_id == fact_id))
        if record is None:
            raise ValueError(f'Profile fact not found: {fact_id}')
        if allowed_for_generation is not None:
            record.allowed_for_generation = allowed_for_generation
        if disallowed is not None:
            record.disallowed = disallowed
        session.flush()
        session.refresh(record)
        return PersonalFactRecordView.from_record(record)


def delete_personal_fact(runtime, fact_id: str) -> PersonalFactRecordView:
    with runtime.session_scope() as session:
        record = session.scalar(select(ProfileFactRecord).where(ProfileFactRecord.fact_id == fact_id))
        if record is None:
            raise ValueError(f'Profile fact not found: {fact_id}')
        payload = PersonalFactRecordView.from_record(record)
        session.delete(record)
        session.flush()
        return payload
