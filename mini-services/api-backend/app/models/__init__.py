"""SQLAlchemy ORM models package (task #3 split from a single 1924-line
app/models.py - moved verbatim into thematic submodules, no behavior
change: this __init__ re-exports every model class so every existing
`from .models import X` / `from ..models import X` import keeps working
unchanged, and Base.metadata is still the one object every class
registers on (imported from _shared, which imports it from app.db).

Graphs are stored as JSON (JSONB on PostgreSQL via variant) so the
visual canvas document maps 1:1 to a database row - one graph, one row.
"""

from __future__ import annotations

from ._shared import Base, JSONVariant, _now, _uuid
from .core import User, ApiKey
from .workflows import Workflow, ExecutionLog, WorkflowVersion, NotificationRule, PackRegistry
from .resources import Credential, CredentialEvent, EnvVariable, AppSetting, Folder, AgentMemory
from .datasets import Dataset, DatasetVersion, DatasetContract, IngestionState, DatasetContractRevision
from .apps import Artifact, TrainedModel, App, Dashboard, AppShareGrant, GrantAuditEvent, DashboardAuditEvent
from .reports import ScheduledReport, ReportDeliveryEvent, ChainReportSchedule
from .systems import SystemDraft, Solution, Py8nSystem, SystemOperation, SystemComponent, SystemMember, SystemEvent, SystemDeployment, SystemApiKey
from .model_deployments import ModelSystem, ModelSystemComponent, ModelDeployment, DeploymentToken, DeploymentRevision, DeploymentTokenPolicy, DeploymentTokenHit
from .interactions import InteractionConversation, InteractionMessage, ChannelEndpoint, ChannelQueue, ChannelQueueEntry
from .voice import VoiceSession, VoiceMeeting, VoiceMeetingParticipant, VoiceCampaign, VoiceCampaignTarget, VoiceEvent, VoiceMeetingMessage, VoiceMeetingRecording, VoiceAgent, MediaSession
from .business import BusinessProcess, BusinessProcessInstance, BusinessProcessTransitionLog

__all__ = [
    "Base", "JSONVariant",
    "User",
    "ApiKey",
    "Workflow",
    "ExecutionLog",
    "WorkflowVersion",
    "NotificationRule",
    "PackRegistry",
    "Credential",
    "CredentialEvent",
    "EnvVariable",
    "AppSetting",
    "Folder",
    "AgentMemory",
    "Dataset",
    "DatasetVersion",
    "DatasetContract",
    "IngestionState",
    "DatasetContractRevision",
    "Artifact",
    "TrainedModel",
    "App",
    "Dashboard",
    "AppShareGrant",
    "GrantAuditEvent",
    "DashboardAuditEvent",
    "ScheduledReport",
    "ReportDeliveryEvent",
    "ChainReportSchedule",
    "SystemDraft",
    "Solution",
    "Py8nSystem",
    "SystemOperation",
    "SystemComponent",
    "SystemMember",
    "SystemEvent",
    "SystemDeployment",
    "SystemApiKey",
    "ModelSystem",
    "ModelSystemComponent",
    "ModelDeployment",
    "DeploymentToken",
    "DeploymentRevision",
    "DeploymentTokenPolicy",
    "DeploymentTokenHit",
    "InteractionConversation",
    "InteractionMessage",
    "ChannelEndpoint",
    "ChannelQueue",
    "ChannelQueueEntry",
    "VoiceSession",
    "VoiceMeeting",
    "VoiceMeetingParticipant",
    "VoiceCampaign",
    "VoiceCampaignTarget",
    "VoiceEvent",
    "VoiceMeetingMessage",
    "VoiceMeetingRecording",
    "VoiceAgent",
    "MediaSession",
    "BusinessProcess",
    "BusinessProcessInstance",
    "BusinessProcessTransitionLog",
]
