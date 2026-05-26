from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class TenantPlan(StrEnum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class Tenant(BaseModel):
    id: str  # tnt_<12 hex>
    name: str
    owner_email: str
    plan: TenantPlan = TenantPlan.FREE
    status: TenantStatus = TenantStatus.ACTIVE
    created_at: datetime
    updated_at: datetime


class TenantCreate(BaseModel):
    name: str
    owner_email: str
    plan: TenantPlan | None = None


class TenantUpdate(BaseModel):
    name: str | None = None
    owner_email: str | None = None
    plan: TenantPlan | None = None
    status: TenantStatus | None = None
