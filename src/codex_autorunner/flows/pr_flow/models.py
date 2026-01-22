from enum import Enum
from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class TargetType(str, Enum):
    ISSUE = "issue"
    PR = "pr"


class PrFlowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_type: TargetType = Field(..., description="Type of input (issue or pr)")
    issue_url: Optional[str] = Field(None, description="GitHub issue URL")
    pr_url: Optional[str] = Field(None, description="GitHub PR URL")
    branch_name: Optional[str] = Field(None, description="Branch name for new PR")


class PrFlowState(BaseModel):
    model_config = ConfigDict(extra="allow")

    target_type: Optional[TargetType] = None
    target_url: Optional[str] = None
    owner: Optional[str] = None
    repo: Optional[str] = None
    issue_number: Optional[int] = None
    pr_number: Optional[int] = None
    branch: Optional[str] = None
    commit_sha: Optional[str] = None
    workspace_path: Optional[str] = None
    spec_path: Optional[str] = None
    progress_path: Optional[str] = None
    cycle_count: int = 0
    feedback_count: int = 0
    last_sync_sha: Optional[str] = None
    artifacts: Dict[str, str] = Field(default_factory=dict)
