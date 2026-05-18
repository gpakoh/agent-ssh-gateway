"""Pydantic models for API requests and responses."""

from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

class ConnectRequest(BaseModel):
    """Request body for creating an SSH connection."""

    host: str = Field(..., min_length=1, description="Target hostname or IP")
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1)
    password: Optional[str] = Field(default=None)
    private_key: Optional[str] = Field(default=None)
    key_passphrase: Optional[str] = Field(default=None)

    @model_validator(mode="after")
    def check_auth_method(self):
        """Ensure at least one auth method is provided."""
        if not self.password and not self.private_key:
            raise ValueError("Either password or private_key must be provided")
        return self

    def __repr__(self) -> str:
        return f"ConnectRequest(host={self.host!r}, port={self.port}, username={self.username!r})"

    def __str__(self) -> str:
        return self.__repr__()


class ConnectResponse(BaseModel):
    """Response after successful SSH connection."""

    session_id: str
    status: str = "connected"
    message: str = "SSH session established successfully"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class ExecuteRequest(BaseModel):
    """Request body for executing a command."""

    session_id: str = Field(..., min_length=1)
    command: str = Field(..., min_length=1)
    timeout: int = Field(default=30, ge=1, le=3600)


class ExecuteResponse(BaseModel):
    """Response after command execution."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    duration: float = 0.0


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

class DisconnectRequest(BaseModel):
    """Request body for closing a session."""

    session_id: str = Field(..., min_length=1)


class DisconnectResponse(BaseModel):
    """Response after disconnecting."""

    status: str = "disconnected"
    message: str = "Session closed successfully"


class SessionInfo(BaseModel):
    """Information about an active session."""

    session_id: str
    host: str
    port: int
    username: str
    connected_at: str
    last_activity: str


class SessionsResponse(BaseModel):
    """Response with list of active sessions."""

    sessions: list[SessionInfo]
    count: int


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "ok"


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    """Error response."""

    detail: str
    error_type: Optional[str] = None


# ---------------------------------------------------------------------------
# Background Jobs
# ---------------------------------------------------------------------------

class JobRunRequest(BaseModel):
    """Request to start a background job."""

    session_id: str = Field(..., min_length=1)
    command: str = Field(..., min_length=1)
    timeout: int = Field(default=3600, ge=1, le=7200)


class JobRunResponse(BaseModel):
    """Response after starting a background job."""

    job_id: str
    status: str = "pending"
    message: str = "Job started"


class JobStatusResponse(BaseModel):
    """Job status response."""

    job_id: str
    status: str
    progress: dict = Field(default_factory=dict)
    duration: Optional[float] = None


class JobResultResponse(BaseModel):
    """Full job result response."""

    job_id: str
    session_id: str
    command: str
    status: str
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    duration: Optional[float] = None
    error_message: Optional[str] = None
    progress: dict = Field(default_factory=dict)


class JobListResponse(BaseModel):
    """List of jobs response."""

    jobs: list[JobResultResponse]
    count: int


# ---------------------------------------------------------------------------
# File Edit
# ---------------------------------------------------------------------------

class EditOperation(BaseModel):
    """Single file edit operation."""

    type: str = Field(..., pattern="^(replace|insert_after|insert_before|delete|append)$")
    old: Optional[str] = Field(default=None)
    new: Optional[str] = Field(default=None)
    after: Optional[str] = Field(default=None)
    before: Optional[str] = Field(default=None)
    text: Optional[str] = Field(default=None)
    count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def check_fields(self):
        if self.type == "replace" and self.old is None:
            raise ValueError("replace requires 'old'")
        if self.type == "insert_after" and self.after is None:
            raise ValueError("insert_after requires 'after'")
        if self.type == "insert_before" and self.before is None:
            raise ValueError("insert_before requires 'before'")
        return self


class FileEditRequest(BaseModel):
    """Request to edit a remote file."""

    session_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    operations: list[EditOperation] = Field(..., min_length=1)


class FileEditResponse(BaseModel):
    """Response after editing a file."""

    success: bool = True
    path: str
    operations_applied: int
    changed: bool


class FileReadRequest(BaseModel):
    """Request to read a remote file."""

    session_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)


class FileReadResponse(BaseModel):
    """Response with file content."""

    path: str
    content: str


class PatchApplyRequest(BaseModel):
    """Request to apply a patch."""

    session_id: str = Field(..., min_length=1)
    patch: str = Field(..., min_length=1)
    strip: int = Field(default=0, ge=0)


class PatchApplyResponse(BaseModel):
    """Response after applying a patch."""

    success: bool = True
    output: str


# ---------------------------------------------------------------------------
# Context Management
# ---------------------------------------------------------------------------

class ContextCreateRequest(BaseModel):
    """Request to create a development context."""

    session_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, description="Context name (e.g., 'gateway_refactor')")
    path: str = Field(..., min_length=1, description="Working directory path")
    branch: Optional[str] = Field(default=None, description="Git branch to checkout/create")
    auto_commit: bool = Field(default=False, description="Auto-commit on successful edits")
    auto_validate: bool = Field(default=False, description="Auto-run mypy+tests after edits")


class GitInfoResponse(BaseModel):
    """Git repository information."""

    status: str
    branch: Optional[str] = None
    has_changes: bool = False
    last_commit: Optional[str] = None
    remote_url: Optional[str] = None
    message: str = ""
    can_commit: bool = False


class TabStateResponse(BaseModel):
    """Tab (open file) state."""
    path: str
    active: bool
    cursor: dict = Field(default_factory=dict)
    scroll_position: int = 0
    view_mode: str = "text"


class SmartContextStateResponse(BaseModel):
    """Smart context state."""
    tabs: list[TabStateResponse]
    active_tab: Optional[str] = None
    command_history: list[dict] = Field(default_factory=list)
    search_history: list[dict] = Field(default_factory=list)
    bookmarks: list[dict] = Field(default_factory=list)
    last_edited_file: Optional[str] = None
    clipboard_size: int = 0


class ContextResponse(BaseModel):
    """Response with context information."""

    context_id: str
    name: str
    path: str
    session_id: str
    branch: Optional[str] = None
    git: GitInfoResponse
    auto_commit: bool = False
    auto_validate: bool = False
    files_opened: list[str] = Field(default_factory=list)
    smart_state: SmartContextStateResponse
    created_at: float
    message: str


class ContextListResponse(BaseModel):
    """Response with list of contexts."""

    contexts: list[ContextResponse]
    count: int


class ContextActionRequest(BaseModel):
    """Request for context action."""

    context_id: str = Field(..., min_length=1)


class GitInitRequest(BaseModel):
    """Request to initialize git repository."""

    context_id: str = Field(..., min_length=1)
    remote_url: Optional[str] = Field(default=None, description="Remote repository URL")


class GitCommitRequest(BaseModel):
    """Request to create a git commit."""

    context_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    files: Optional[list[str]] = Field(default=None, description="Specific files to commit")


class GitActionResponse(BaseModel):
    """Response for git actions."""

    success: bool
    message: str
    error: Optional[str] = None
    hash: Optional[str] = None


class FileEditWithContextRequest(BaseModel):
    """Request to edit a file with context awareness."""

    context_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    operations: list[EditOperation] = Field(..., min_length=1)
    run_validation: bool = Field(default=False, description="Run validation after edit")
    commit_message: Optional[str] = Field(default=None, description="Auto-commit message")


class ValidationStepResult(BaseModel):
    """Result of a single validation step."""

    name: str
    status: str
    output: str = ""
    errors: int = 0
    warnings: int = 0
    duration: float = 0.0


class ValidationReportResponse(BaseModel):
    """Full validation report."""

    overall_status: str
    summary: str
    total_duration: float
    can_commit: bool
    steps: list[ValidationStepResult]


class FileEditWithContextResponse(BaseModel):
    """Response after editing a file with context."""

    success: bool = True
    path: str
    operations_applied: int
    changed: bool
    git_commit: Optional[str] = None
    validation_result: Optional[ValidationReportResponse] = None
    warning: Optional[str] = None


class ValidateRequest(BaseModel):
    """Request to run validation."""

    context_id: str = Field(..., min_length=1)
    run_mypy: bool = Field(default=True)
    run_tests: bool = Field(default=True)


# ---------------------------------------------------------------------------
# Smart Context
# ---------------------------------------------------------------------------

class OpenFileRequest(BaseModel):
    """Request to open file in smart context."""

    context_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)


class CloseFileRequest(BaseModel):
    """Request to close file in smart context."""

    context_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)


class UpdateCursorRequest(BaseModel):
    """Request to update cursor position."""

    context_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    line: int = Field(..., ge=1)
    column: int = Field(default=1, ge=1)


class AddCommandRequest(BaseModel):
    """Request to add command to history."""

    context_id: str = Field(..., min_length=1)
    command: str = Field(..., min_length=1)
    directory: str = Field(default="")


class AddSearchRequest(BaseModel):
    """Request to add search query."""

    context_id: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1)
    path: str = Field(default="")
    replace_with: str = Field(default="")


class AddBookmarkRequest(BaseModel):
    """Request to add bookmark."""

    context_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    line: int = Field(..., ge=1)
    note: str = Field(default="")


class RemoveBookmarkRequest(BaseModel):
    """Request to remove bookmark."""

    context_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    line: int = Field(..., ge=1)


# ---------------------------------------------------------------------------
# Batch Operations
# ---------------------------------------------------------------------------

class BatchOperation(BaseModel):
    """Single batch operation."""

    type: str = Field(..., pattern="^(read|edit|create|delete|rename|copy|execute)$")
    path: str = Field(default="", description="File path (relative to context path)")
    operations: list[EditOperation] = Field(default_factory=list, description="Edit operations (for type=edit)")
    content: str = Field(default="", description="File content (for type=create)")
    new_path: str = Field(default="", description="New path (for type=rename)")
    dest_path: str = Field(default="", description="Destination path (for type=copy)")
    command: str = Field(default="", description="Shell command (for type=execute)")
    continue_on_error: bool = Field(default=False, description="Continue if this operation fails")


class BatchExecuteRequest(BaseModel):
    """Request to execute batch operations."""

    context_id: str = Field(..., min_length=1)
    operations: list[BatchOperation] = Field(..., min_length=1, max_length=50)
    auto_commit: bool = Field(default=False, description="Auto-commit all changes")
    commit_message: str = Field(default="", description="Commit message")
    run_validation: bool = Field(default=False, description="Run validation after all operations")


class BatchOperationResultResponse(BaseModel):
    """Result of single batch operation."""

    operation: str
    path: str
    success: bool
    output: str = ""
    error: Optional[str] = None
    duration: float = 0.0
    lines_changed: int = 0


class BatchExecuteResponse(BaseModel):
    """Response after batch operations."""

    transaction_id: str
    overall_success: bool
    summary: str
    total_duration: float
    operations: list[BatchOperationResultResponse]
    git_commit: Optional[str] = None
    validation_result: Optional[dict] = None


class BatchReadRequest(BaseModel):
    """Request to read multiple files."""

    session_id: str = Field(..., min_length=1)
    paths: list[str] = Field(..., min_length=1, max_length=20)


class BatchReadResponse(BaseModel):
    """Response with multiple file contents."""

    files: dict[str, str] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Code Intelligence
# ---------------------------------------------------------------------------

class CodeSearchRequest(BaseModel):
    """Request to search code."""

    session_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1, description="Directory to search in")
    query: str = Field(..., min_length=1)
    language: str = Field(default="py", description="File extension without dot")
    context_lines: int = Field(default=3, ge=0, le=10)


class CodeSearchResultItem(BaseModel):
    """Single search result."""

    path: str
    line: int
    column: int
    content: str


class CodeSearchResponse(BaseModel):
    """Response for code search."""

    query: str
    results: list[CodeSearchResultItem]
    count: int


class CodeInsertRequest(BaseModel):
    """Request to insert code intelligently."""

    context_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    instruction: str = Field(..., min_length=1, description="Natural language instruction")
    language: str = Field(default="python")
    auto_commit: bool = Field(default=False)


class CodeInsertSuggestion(BaseModel):
    """Code insertion suggestion."""

    insert_after: str
    code: str
    explanation: str
    line_number: int


class CodeInsertResponse(BaseModel):
    """Response after intelligent insertion."""

    success: bool
    path: str
    suggestion: CodeInsertSuggestion
    applied: bool = False
    git_commit: Optional[str] = None


class CodeGenerateRequest(BaseModel):
    """Request to generate code."""

    instruction: str = Field(..., min_length=1)
    language: str = Field(default="python")


class CodeGenerateResponse(BaseModel):
    """Response with generated code."""

    code: str
    language: str
    explanation: str


class CodeCompleteRequest(BaseModel):
    """Request for code completion."""

    session_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    partial_code: str = Field(..., min_length=1)
    language: str = Field(default="python")


class CodeCompleteResponse(BaseModel):
    """Response with completion suggestion."""

    completion: str
    context: str


# ---------------------------------------------------------------------------
# Error Recovery
# ---------------------------------------------------------------------------

class CreateBackupRequest(BaseModel):
    """Request to create a backup."""

    context_id: str = Field(..., min_length=1)
    name: str = Field(default="auto_backup", description="Backup name")


class RestoreBackupRequest(BaseModel):
    """Request to restore from backup."""

    context_id: str = Field(..., min_length=1)
    backup_id: Optional[str] = Field(default=None, description="Specific backup ID or latest")


class BackupInfo(BaseModel):
    """Backup information."""

    id: str
    name: str
    created_at: float
    files_changed: list[str] = Field(default_factory=list)


class ListBackupsResponse(BaseModel):
    """Response with list of backups."""

    backups: list[BackupInfo]
    count: int


class RecoveryActionResponse(BaseModel):
    """Response for recovery actions."""

    success: bool
    message: str
    backup_id: Optional[str] = None
    restored_files: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Global Search & Replace
# ---------------------------------------------------------------------------

class GlobalSearchRequest(BaseModel):
    """Request for global search."""

    session_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1)
    file_pattern: str = Field(default="*", description="File glob pattern")
    use_regex: bool = Field(default=False)
    case_sensitive: bool = Field(default=True)
    context_lines: int = Field(default=2, ge=0, le=5)


class SearchMatchItem(BaseModel):
    """Single search match."""

    path: str
    line: int
    column: int
    content: str


class GlobalSearchResponse(BaseModel):
    """Response for global search."""

    query: str
    matches: list[SearchMatchItem]
    total_count: int
    files_affected: list[str]


class GlobalReplaceRequest(BaseModel):
    """Request for global replace."""

    session_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    search: str = Field(..., min_length=1)
    replace: str = Field(default="")
    file_pattern: str = Field(default="*")
    use_regex: bool = Field(default=False)
    case_sensitive: bool = Field(default=True)
    dry_run: bool = Field(default=False, description="Preview changes without applying")
    auto_commit: bool = Field(default=False)
    context_id: Optional[str] = Field(default=None)


class ReplaceResultItem(BaseModel):
    """Single replace result."""

    path: str
    replacements_count: int
    success: bool
    error: Optional[str] = None


class GlobalReplaceResponse(BaseModel):
    """Response for global replace."""

    search: str
    replace: str
    results: list[ReplaceResultItem]
    total_replacements: int
    files_modified: int
    dry_run: bool
    git_commit: Optional[str] = None


# ---------------------------------------------------------------------------
# File Tree Explorer
# ---------------------------------------------------------------------------

class FileTreeRequest(BaseModel):
    """Request to get file tree."""

    session_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    depth: int = Field(default=2, ge=1, le=5)
    show_hidden: bool = Field(default=False)
    max_files: int = Field(default=100, ge=10, le=1000)


class FileTreeNode(BaseModel):
    """Node in file tree."""

    name: str
    path: str
    type: str
    size: int = 0
    permissions: str = ""
    modified_at: str = ""
    children: list["FileTreeNode"] = []


class FileTreeResponse(BaseModel):
    """Response with file tree."""

    root: FileTreeNode
    total_files: int
    total_directories: int


# ---------------------------------------------------------------------------
# Template Library
# ---------------------------------------------------------------------------

class TemplateInfo(BaseModel):
    """Template information."""

    id: str
    name: str
    description: str
    language: str


class TemplateListResponse(BaseModel):
    """Response with list of templates."""

    templates: list[TemplateInfo]
    count: int


class TemplateGetRequest(BaseModel):
    """Request to get template."""

    template_id: str = Field(..., min_length=1)


class TemplateRenderRequest(BaseModel):
    """Request to render template."""

    context_id: str = Field(..., min_length=1)
    template_id: str = Field(..., min_length=1)
    params: dict = Field(default_factory=dict)
    target_path: str = Field(..., min_length=1)
    auto_commit: bool = Field(default=False)


class TemplateRenderResponse(BaseModel):
    """Response after rendering template."""

    success: bool
    template_id: str
    target_path: str
    code: str
    git_commit: Optional[str] = None


# ---------------------------------------------------------------------------
# Smart Diff
# ---------------------------------------------------------------------------

class DiffLine(BaseModel):
    """Single diff line."""

    type: str  # equal, added, removed
    old_line: Optional[int] = None
    new_line: Optional[int] = None
    content: str


class DiffResponse(BaseModel):
    """Response with diff information."""

    unified_diff: str
    inline_diff: list[DiffLine]
    changes: dict
    old_path: str
    new_path: str


class FileEditWithContextResponse(BaseModel):
    """Response after editing a file with context."""

    success: bool = True
    path: str
    operations_applied: int
    changed: bool
    git_commit: Optional[str] = None
    validation_result: Optional[ValidationReportResponse] = None
    warning: Optional[str] = None
    diff: Optional[DiffResponse] = None


# ---------------------------------------------------------------------------
# Project Analytics
# ---------------------------------------------------------------------------

class ProjectAnalyticsRequest(BaseModel):
    """Request to analyze project."""

    session_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)


class FileStats(BaseModel):
    """File statistics."""

    total_files: int
    total_directories: int
    extensions: dict[str, int]


class CodeStats(BaseModel):
    """Code statistics."""

    python_lines_of_code: int
    classes: int
    functions: int


class GitStats(BaseModel):
    """Git statistics."""

    is_git_repo: bool
    total_commits: int = 0
    branches: int = 0
    contributors: int = 0
    last_commit: str = ""


class TestStats(BaseModel):
    """Test statistics."""

    test_files: int
    total_tests: int
    has_tests: bool


class DependencyStats(BaseModel):
    """Dependency statistics."""

    requirements_count: int
    has_pyproject: bool
    outdated_packages: int


class ProjectAnalyticsResponse(BaseModel):
    """Response with project analytics."""

    project_path: str
    files: FileStats
    code: CodeStats
    git: GitStats
    tests: TestStats
    dependencies: DependencyStats


# ---------------------------------------------------------------------------
# Server Management
# ---------------------------------------------------------------------------

class ServerInfo(BaseModel):
    """Server information."""

    id: str
    name: str
    host: str
    port: int
    username: str
    description: str
    tags: list[str]
    status: str
    last_check: Optional[float] = None
    has_session: bool = False


class ServerListResponse(BaseModel):
    """Response with server list."""

    servers: list[ServerInfo]
    count: int


class AddServerRequest(BaseModel):
    """Request to add server."""

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    host: str = Field(..., min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default="root")
    description: str = Field(default="")
    tags: list[str] = Field(default_factory=list)


class ConnectServerRequest(BaseModel):
    """Request to connect to server."""

    server_id: str = Field(..., min_length=1)
    password: Optional[str] = Field(default=None)
    private_key: Optional[str] = Field(default=None)


class ServerConnectResponse(BaseModel):
    """Response after connecting to server."""

    server_id: str
    session_id: str
    status: str
    message: str


# ---------------------------------------------------------------------------
# PTY (Interactive Terminal)
# ---------------------------------------------------------------------------

class PTYCreateRequest(BaseModel):
    """Request to create PTY session."""

    session_id: str = Field(..., min_length=1)
    term: str = Field(default="xterm-256color")
    rows: int = Field(default=24, ge=1)
    cols: int = Field(default=80, ge=1)


class PTYInputRequest(BaseModel):
    """Request to send input to PTY."""

    data: str = Field(..., min_length=1)


class PTYOutputResponse(BaseModel):
    """Response with PTY output."""

    output: str
    eof: bool = False


class PTYCloseRequest(BaseModel):
    """Request to close PTY."""

    session_id: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Snapshot System
# ---------------------------------------------------------------------------

class SnapshotInfo(BaseModel):
    """Snapshot information."""

    id: str
    name: str
    context_id: str
    created_at: float
    files: list[str]
    description: str
    git_commit_before: Optional[str] = None
    size_bytes: int = 0


class CreateSnapshotRequest(BaseModel):
    """Request to create snapshot."""

    context_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    description: str = Field(default="")


class RestoreSnapshotRequest(BaseModel):
    """Request to restore snapshot."""

    context_id: str = Field(..., min_length=1)
    snapshot_id: str = Field(..., min_length=1)


class SnapshotListResponse(BaseModel):
    """Response with snapshot list."""

    snapshots: list[SnapshotInfo]
    count: int


class SnapshotActionResponse(BaseModel):
    """Response for snapshot actions."""

    success: bool
    message: str
    snapshot_id: Optional[str] = None
    restored_files: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# CI/CD Webhooks
# ---------------------------------------------------------------------------

class WebhookConfigResponse(BaseModel):
    """Webhook configuration."""

    id: str
    name: str
    webhook_type: str
    target_path: str
    deploy_command: str
    context_id: str
    notify_url: Optional[str] = None
    enabled: bool


class WebhookListResponse(BaseModel):
    """Response with webhook list."""

    webhooks: list[WebhookConfigResponse]
    count: int


class CreateWebhookRequest(BaseModel):
    """Request to create webhook."""

    name: str = Field(..., min_length=1)
    webhook_type: str = Field(default="generic")
    secret: str = Field(default="")
    target_path: str = Field(..., min_length=1)
    deploy_command: str = Field(..., min_length=1)
    context_id: str = Field(..., min_length=1)
    notify_url: Optional[str] = Field(default=None)


class DeployRequest(BaseModel):
    """Request to trigger deployment."""

    webhook_id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)


class DeployResponse(BaseModel):
    """Response after deployment trigger."""

    status: str
    job_id: Optional[str] = None
    message: str


class DeploymentInfo(BaseModel):
    """Deployment information."""

    id: str
    webhook_id: str
    webhook_name: str
    status: str
    timestamp: float
