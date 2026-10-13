from enum import Enum
from typing import Any, Dict, List, Optional
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


def validate_changes(value):
    if not isinstance(value, dict) or not value or set(value) - {'strategy', 'request_retry', 'request_log'}:
        raise ValueError('Invalid configuration changes')
    if 'strategy' in value and value['strategy'] not in ('round-robin', 'fill-first'):
        raise ValueError('Invalid strategy')
    if 'request_retry' in value and (type(value['request_retry']) is not int or not 0 <= value['request_retry'] <= 10):
        raise ValueError('Invalid retry')
    if 'request_log' in value and type(value['request_log']) is not bool:
        raise ValueError('Invalid logging')
    return value

class LifecycleState(str, Enum):
    UNCONFIGURED = "unconfigured"
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    FAILED = "failed"
    EXTERNAL_UNREACHABLE = "external_unreachable"
    EXTERNAL_READY = "external_ready"
    OWNERSHIP_LOST = "ownership_lost"

class ErrorCode(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    SECRET_UNAVAILABLE = "SECRET_UNAVAILABLE"
    POLICY_LOCKED = "POLICY_LOCKED"
    INCOMPATIBLE = "INCOMPATIBLE"
    PORT_IN_USE = "PORT_IN_USE"
    UNOWNED = "UNOWNED"
    STALE_REVISION = "STALE_REVISION"
    BUSY = "BUSY"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID_INPUT = "INVALID_INPUT"

class ApiErrorEnvelope(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    operation_id: Optional[str] = None

class ApiResponseEnvelope(BaseModel):
    api_version: int = 1
    scope_id: str
    generation: int
    revision: str
    data: Optional[Any] = None
    error: Optional[ApiErrorEnvelope] = None

class StatusData(BaseModel):
    mode: str
    lifecycle: LifecycleState
    instance_id: str
    cpa_version: str
    console_version: str
    management_ready: bool
    inference_configured: Optional[bool]
    console_compatible: bool
    last_transition_time: float
    sanitized_reason: Optional[str] = None
    supported_operations: List[str] = Field(default_factory=list)
    pending_operation: Optional[str] = None
    pid: Optional[int] = None

class OperationRequest(StrictRequest):
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_-]+$')
    expected_generation: int = Field(ge=1)
    action: Literal['start', 'stop', 'restart']

class OperationRecord(BaseModel):
    operation_id: str
    action: str
    state: str  # pending, running, succeeded, failed
    created_at: float
    completed_at: Optional[float] = None
    error: Optional[str] = None

class ConfigProjection(BaseModel):
    strategy: str = "round-robin"
    request_retry: int = 3
    request_log: bool = False
    revision: str
    restart_required: bool = True
    locked_invariants: List[str] = Field(default_factory=lambda: [
        'server.host', 'server.discovery', 'management', 'access', 'oauth'])

class ConfigApplyRequest(StrictRequest):
    expected_revision: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    expected_generation: int = Field(ge=1)
    changes: Dict[str, Any]
    restart_consent: bool = False

    _changes = field_validator('changes')(validate_changes)

class EventRecord(BaseModel):
    seq: int
    time: float
    scope_id: str
    generation: int
    type: str
    summary_code: str
    operation_id: Optional[str] = None
    revision: Optional[str] = None
