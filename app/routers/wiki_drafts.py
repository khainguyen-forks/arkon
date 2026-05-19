"""
Wiki Draft router — propose, review, approve, and reject wiki page drafts.

Permission model:
  - Propose (POST /drafts): workspace contributor+ OR global wiki:write
  - Review/Approve/Reject: workspace editor+ OR wiki:write:all OR admin
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import and_, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.database.models import (
    Employee,
    ProjectMember,
    WikiDraftRound,
    WikiPage,
    WikiPageDraft,
    WorkspaceRole,
)
from app.services import contribution_service, wiki_service
from app.services.audit_service import log_audit
from app.services.auth_service import get_current_user, require_permission
from app.services.contribution_service import (
    InvalidTransition,
    wiki_draft_adapter,
)
from app.services.permission_engine import (
    _get_user_permissions,
    get_workspace_role,
    has_any_permission,
    workspace_role_can,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ProposeDraftRequest(BaseModel):
    content_md: str
    note: Optional[str] = None
    base_version: Optional[int] = None
    scope_type: Optional[str] = None
    scope_id: Optional[uuid.UUID] = None

    @field_validator("content_md")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("content_md must not be empty")
        if len(v) > 50_000:
            raise ValueError("content_md exceeds 50,000 character limit")
        return v


class ApproveDraftRequest(BaseModel):
    reviewer_note: Optional[str] = None
    edited_content_md: Optional[str] = None
    allow_conflict: bool = False


class RejectDraftRequest(BaseModel):
    reviewer_note: str

    @field_validator("reviewer_note")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("reviewer_note is required when rejecting")
        return v


class RequestChangesRequest(BaseModel):
    reviewer_note: str

    @field_validator("reviewer_note")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("reviewer_note is required when requesting changes")
        return v


class ResubmitDraftRequest(BaseModel):
    content_md: str
    note: Optional[str] = None

    @field_validator("content_md")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("content_md must not be empty")
        if len(v) > 50_000:
            raise ValueError("content_md exceeds 50,000 character limit")
        return v


class DraftRoundResponse(BaseModel):
    id: uuid.UUID
    round_no: int
    content_md: str
    author_note: Optional[str]
    reviewer_return_note: Optional[str]
    submitted_at: str


class DraftResponse(BaseModel):
    id: uuid.UUID
    page_id: uuid.UUID
    page_slug: str
    page_title: str
    page_version: int
    base_version: Optional[int] = None
    has_conflict: bool = False
    author_id: Optional[uuid.UUID]
    author_name: Optional[str]
    content_md: str
    note: Optional[str]
    status: str
    revision_round: int = 0
    last_returned_note: Optional[str] = None
    source: str
    reviewed_by_name: Optional[str] = None
    reviewed_at: Optional[str] = None
    reviewer_note: Optional[str] = None
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _can_propose(db: AsyncSession, user: Employee, page: WikiPage) -> bool:
    """Contributor+ in workspace, or any wiki:write for global pages."""
    if user.role == "admin":
        return True
    if page.scope_type == "project" and page.scope_id:
        role = await get_workspace_role(db, user, page.scope_id)
        return bool(role) and workspace_role_can(role, "contributor")
    perms = _get_user_permissions(user)
    return has_any_permission(list(perms), "wiki", "write")


async def _can_review(db: AsyncSession, user: Employee, page: WikiPage) -> bool:
    """Editor+ in workspace, or wiki:write:all, or admin."""
    if user.role == "admin":
        return True
    if page.scope_type == "project" and page.scope_id:
        role = await get_workspace_role(db, user, page.scope_id)
        return bool(role) and workspace_role_can(role, "editor")
    perms = _get_user_permissions(user)
    return "wiki:write:all" in perms


def _build_reviewable_page_filter(user: Employee):
    """SQL filter selecting WikiPage rows the user can review (editor+).

    Returns None if the user can review everything (admin / wiki:write:all),
    a falsy filter if they can review nothing, otherwise an OR clause covering
    project-scoped pages the user is editor+ in (no global/department review).
    """
    if user.role == "admin":
        return None
    perms = _get_user_permissions(user)
    can_global = "wiki:write:all" in perms
    if can_global:
        return None

    editor_levels = [WorkspaceRole.EDITOR.value, WorkspaceRole.ADMIN.value]
    workspace_pages = select(ProjectMember.project_id).where(
        ProjectMember.employee_id == user.id,
        ProjectMember.role.in_(editor_levels),
    )
    return and_(
        WikiPage.scope_type == "project",
        WikiPage.scope_id.in_(workspace_pages),
    )


async def _load_draft(db: AsyncSession, draft_id: str) -> WikiPageDraft:
    try:
        did = uuid.UUID(draft_id)
    except ValueError:
        raise HTTPException(400, "Invalid draft ID format")
    draft = await db.get(WikiPageDraft, did)
    if not draft:
        raise HTTPException(404, f"Draft {draft_id} not found")
    return draft


async def _draft_response(db: AsyncSession, draft: WikiPageDraft) -> DraftResponse:
    page = await db.get(WikiPage, draft.page_id)
    author = await db.get(Employee, draft.author_id) if draft.author_id else None
    reviewer = await db.get(Employee, draft.reviewed_by_id) if draft.reviewed_by_id else None
    current_version = page.version if page else 1
    has_conflict = bool(
        draft.status == "pending"
        and draft.base_version is not None
        and current_version is not None
        and draft.base_version < current_version
    )
    return DraftResponse(
        id=draft.id,
        page_id=draft.page_id,
        page_slug=page.slug if page else "",
        page_title=page.title if page else "",
        page_version=current_version or 1,
        base_version=draft.base_version,
        has_conflict=has_conflict,
        author_id=draft.author_id,
        author_name=author.name if author else None,
        content_md=draft.content_md,
        note=draft.note,
        status=draft.status,
        revision_round=draft.revision_round or 0,
        last_returned_note=draft.last_returned_note,
        source=draft.source,
        reviewed_by_name=reviewer.name if reviewer else None,
        reviewed_at=draft.reviewed_at.isoformat() if draft.reviewed_at else None,
        reviewer_note=draft.reviewer_note,
        created_at=draft.created_at.isoformat(),
        updated_at=draft.updated_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/wiki/pages/{slug:path}/drafts", response_model=DraftResponse, status_code=201)
async def propose_draft(
    slug: str,
    body: ProposeDraftRequest,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Propose an edit to an existing wiki page. Creates a pending draft for editor review."""
    if slug in (wiki_service.INDEX_SLUG, wiki_service.LOG_SLUG):
        raise HTTPException(400, "Cannot propose drafts for reserved pages")

    if body.scope_type is not None:
        page = await wiki_service.get_page_by_slug(
            db, slug, scope_type=body.scope_type, scope_id=body.scope_id,
        )
    else:
        page = await wiki_service.get_page_by_slug_any_scope(db, slug)
    if not page:
        raise HTTPException(404, f"Wiki page not found: {slug}")

    if not await _can_propose(db, user, page):
        raise HTTPException(403, "Insufficient permission to propose a draft for this page")

    # If client passed base_version, sanity-check it matches a real prior
    # version (≤ current). If omitted, default to the page's current version
    # so future approvals can detect drift.
    base_version = body.base_version if body.base_version is not None else page.version
    if base_version is not None and page.version is not None and base_version > page.version:
        raise HTTPException(400, f"base_version {base_version} is ahead of current page v{page.version}")

    draft = await wiki_service.create_draft(
        db,
        page_id=page.id,
        author_id=user.id,
        content_md=body.content_md,
        note=body.note,
        source="web_ui",
        base_version=base_version,
    )
    # The lazy `draft.page` relationship won't be populated for the freshly
    # created row inside this session — set it manually so the adapter can
    # resolve the page scope without an extra round trip.
    draft.page = page
    await log_audit(db, user, "create", "wiki_draft", str(draft.id), reason=f"draft for: {slug}")
    await contribution_service.notify_submitted(db, wiki_draft_adapter, draft, user)
    await db.commit()
    await db.refresh(draft)
    return await _draft_response(db, draft)


@router.get("/wiki/drafts", response_model=list[DraftResponse])
async def list_all_drafts(
    status: Optional[str] = Query("pending", description="Filter by status: pending | approved | rejected"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: Employee = require_permission("wiki:read"),
):
    """List wiki drafts. Editors see drafts for pages they can review. Admins see all.

    Permission filtering runs in SQL (joined on WikiPage) so pagination is
    correct — previously a post-query filter would silently drop drafts past
    the first `limit` rows.
    """
    page_filter = _build_reviewable_page_filter(user)

    stmt = (
        select(WikiPageDraft)
        .join(WikiPage, WikiPage.id == WikiPageDraft.page_id)
        .options(
            selectinload(WikiPageDraft.page),
            selectinload(WikiPageDraft.author),
            selectinload(WikiPageDraft.reviewer),
        )
        .order_by(WikiPageDraft.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status:
        stmt = stmt.where(WikiPageDraft.status == status)
    if page_filter is False:  # noqa: E712 — never true, kept for symmetry
        return []
    if page_filter is not None:
        stmt = stmt.where(page_filter)

    drafts = (await db.execute(stmt)).scalars().all()
    return [await _draft_response(db, d) for d in drafts]


@router.get("/wiki/pages/{slug:path}/drafts", response_model=list[DraftResponse])
async def list_page_drafts(
    slug: str,
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """List drafts for a specific wiki page."""
    page = await wiki_service.get_page_by_slug_any_scope(db, slug)
    if not page:
        raise HTTPException(404, f"Wiki page not found: {slug}")

    if not await _can_review(db, user, page):
        raise HTTPException(403, "Insufficient permission to view drafts for this page")

    stmt = (
        select(WikiPageDraft)
        .where(WikiPageDraft.page_id == page.id)
        .order_by(WikiPageDraft.created_at.desc())
    )
    if status:
        stmt = stmt.where(WikiPageDraft.status == status)

    drafts = (await db.execute(stmt)).scalars().all()
    return [await _draft_response(db, d) for d in drafts]


@router.get("/wiki/drafts/{draft_id}", response_model=DraftResponse)
async def get_draft(
    draft_id: str,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Get a single draft by ID."""
    draft = await _load_draft(db, draft_id)
    page = await db.get(WikiPage, draft.page_id)
    if not page or not await _can_review(db, user, page):
        raise HTTPException(403, "Insufficient permission to view this draft")
    return await _draft_response(db, draft)


@router.post("/wiki/drafts/{draft_id}/approve", response_model=DraftResponse)
async def approve_draft(
    draft_id: str,
    body: ApproveDraftRequest,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Approve a pending draft. Optionally provide edited content before approving."""
    draft = await _load_draft(db, draft_id)
    if draft.status != "pending":
        raise HTTPException(400, f"Draft is already {draft.status}")

    page = await db.get(WikiPage, draft.page_id)
    if not page or not await _can_review(db, user, page):
        raise HTTPException(403, "Insufficient permission to approve drafts for this page")

    # Authors cannot approve their own drafts (admins exempt).
    if user.role != "admin" and draft.author_id == user.id:
        raise HTTPException(403, "You cannot approve your own draft. Ask another editor to review it.")

    try:
        await wiki_service.approve_draft(
            db, draft, user.id,
            reviewer_note=body.reviewer_note,
            edited_content_md=body.edited_content_md,
            allow_conflict=body.allow_conflict,
        )
    except wiki_service.DraftConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "draft_conflict",
                "message": str(e),
                "current_version": e.current_version,
                "base_version": e.base_version,
                "hint": "Re-submit with allow_conflict=true to overwrite, or supply edited_content_md.",
            },
        )
    await log_audit(db, user, "update", "wiki_draft", str(draft.id), reason=f"approved draft for: {page.slug}")
    # Keep _index and _log fresh after content lands.
    scope_type = page.scope_type or "global"
    scope_id = page.scope_id
    await wiki_service.regenerate_index(db, scope_type=scope_type, scope_id=scope_id)
    await wiki_service.append_log(
        db,
        f"Approved draft for: {page.title} ({page.slug}) → v{page.version} by {user.name or user.email}",
        scope_type=scope_type,
        scope_id=scope_id,
    )
    draft.page = page
    await contribution_service.notify_approved(
        db, wiki_draft_adapter, draft, user, version_label=f"v{page.version}",
    )
    await db.commit()
    await db.refresh(draft)
    return await _draft_response(db, draft)


@router.post("/wiki/drafts/{draft_id}/reject", response_model=DraftResponse)
async def reject_draft(
    draft_id: str,
    body: RejectDraftRequest,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Reject a pending draft. reviewer_note is required."""
    draft = await _load_draft(db, draft_id)
    if draft.status != "pending":
        raise HTTPException(400, f"Draft is already {draft.status}")

    page = await db.get(WikiPage, draft.page_id)
    if not page or not await _can_review(db, user, page):
        raise HTTPException(403, "Insufficient permission to reject drafts for this page")

    await wiki_service.reject_draft(db, draft, user.id, body.reviewer_note)
    await log_audit(db, user, "update", "wiki_draft", str(draft.id), reason=f"rejected draft for: {page.slug}")
    draft.page = page
    await contribution_service.notify_rejected(
        db, wiki_draft_adapter, draft, user, reason=body.reviewer_note,
    )
    await db.commit()
    await db.refresh(draft)
    return await _draft_response(db, draft)


# ---------------------------------------------------------------------------
# needs_revision flow — request changes, resubmit, withdraw, rounds history
# ---------------------------------------------------------------------------

@router.post("/wiki/drafts/{draft_id}/request-changes", response_model=DraftResponse)
async def request_changes_on_draft(
    draft_id: str,
    body: RequestChangesRequest,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Send a pending draft back to the author for revisions.

    Required `reviewer_note` explains what to fix. Author may then PATCH
    `/wiki/drafts/{id}/content` with new content to flip the draft back to
    pending. The draft is not deleted — its `revision_round` increments on
    each resubmission.
    """
    draft = await _load_draft(db, draft_id)
    page = await db.get(WikiPage, draft.page_id)
    if not page or not await _can_review(db, user, page):
        raise HTTPException(403, "Insufficient permission to review drafts for this page")
    # Reviewer ≠ author for this action makes no practical sense (reviewer
    # would just edit), but allow admin and rely on author_id check elsewhere.

    draft.page = page
    try:
        await contribution_service.request_changes(
            db, wiki_draft_adapter, draft, user, body.reviewer_note,
        )
    except InvalidTransition as e:
        raise HTTPException(400, str(e))
    await db.commit()
    await db.refresh(draft)
    return await _draft_response(db, draft)


@router.patch("/wiki/drafts/{draft_id}/content", response_model=DraftResponse)
async def resubmit_draft(
    draft_id: str,
    body: ResubmitDraftRequest,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Author resubmits a draft after a request_changes round.

    Bumps `revision_round`, snapshots the prior submission to
    `wiki_draft_rounds`, clears `last_returned_note`, and flips the status
    back to pending so reviewers can look at it again.
    """
    draft = await _load_draft(db, draft_id)
    page = await db.get(WikiPage, draft.page_id)
    if not page:
        raise HTTPException(404, "Parent wiki page not found")
    draft.page = page

    try:
        await contribution_service.resubmit_wiki_draft(
            db, draft, user, body.content_md.strip(), author_note=body.note,
        )
    except InvalidTransition as e:
        raise HTTPException(400, str(e))
    await db.commit()
    await db.refresh(draft)
    return await _draft_response(db, draft)


@router.post("/wiki/drafts/{draft_id}/withdraw", response_model=DraftResponse)
async def withdraw_draft(
    draft_id: str,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Author withdraws a pending or needs_revision draft. Admin override allowed."""
    draft = await _load_draft(db, draft_id)
    page = await db.get(WikiPage, draft.page_id)
    if not page:
        raise HTTPException(404, "Parent wiki page not found")
    draft.page = page

    try:
        await contribution_service.withdraw(db, wiki_draft_adapter, draft, user)
    except InvalidTransition as e:
        raise HTTPException(403, str(e))
    await db.commit()
    await db.refresh(draft)
    return await _draft_response(db, draft)


@router.get("/wiki/drafts/{draft_id}/rounds", response_model=list[DraftRoundResponse])
async def list_draft_rounds(
    draft_id: str,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """List prior submission rounds for a draft (review-trail audit).

    Visible to: author, reviewers of the page's scope, admin.
    """
    draft = await _load_draft(db, draft_id)
    page = await db.get(WikiPage, draft.page_id)
    if not page:
        raise HTTPException(404, "Parent wiki page not found")
    # Author OR reviewer can see the trail.
    if user.role != "admin" and draft.author_id != user.id:
        if not await _can_review(db, user, page):
            raise HTTPException(403, "Insufficient permission to view this draft's rounds")

    rows = (await db.execute(
        select(WikiDraftRound)
        .where(WikiDraftRound.draft_id == draft.id)
        .order_by(WikiDraftRound.round_no.asc())
    )).scalars().all()
    return [
        DraftRoundResponse(
            id=r.id,
            round_no=r.round_no,
            content_md=r.content_md,
            author_note=r.author_note,
            reviewer_return_note=r.reviewer_return_note,
            submitted_at=r.submitted_at.isoformat(),
        )
        for r in rows
    ]
