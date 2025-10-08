from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field
from enum import Enum


class SectionStatus(str, Enum):
    PENDING = "pending"
    DRAFT = "draft"
    EDUCATION_REVIEW = "education_review"
    ALPHA_REVIEW = "alpha_review"
    REVISION_NEEDED = "revision_needed"
    APPROVED = "approved"


class SectionSpec(BaseModel):
    id: str = Field(description="Unique section identifier")
    title: str = Field(description="Section title")
    description: str = Field(description="What this section should contain")
    ordinal: int = Field(description="Order in the final document")
    constraints: Dict[str, Any] = Field(default_factory=dict, description="Template constraints")


class SectionDraft(BaseModel):
    section_id: str = Field(description="References SectionSpec.id")
    content_md: str = Field(description="Markdown content")
    links: List[str] = Field(default_factory=list, description="All URLs found in content")
    word_count: int = Field(description="Approximate word count")
    citations: List[str] = Field(default_factory=list, description="Bibliography entries")
    wlo_mapping: Dict[str, str] = Field(default_factory=dict, description="WLO alignment notes")
    needs_revision: bool = Field(default=False, description="Whether this section needs revision after review")


class DirectEdit(BaseModel):
    """Represents a direct edit that EDITOR can make without Writer intervention"""
    edit_type: str = Field(description="Type of edit: trim_to_word_count, fix_citation, add_missing_section, fix_header, fix_formatting")
    location: Optional[str] = Field(default=None, description="Where to apply edit: section name, line number, or 'after_X'")
    current_value: Optional[str] = Field(default=None, description="Current text/value to find")
    new_value: Optional[str] = Field(default=None, description="New text/value to replace with")
    target: Optional[Any] = Field(default=None, description="Target value (e.g., word count limit)")
    reason: str = Field(description="Why this edit is being made")


class ReviewNotes(BaseModel):
    reviewer: str = Field(description="Agent name that performed review")
    approved: bool = Field(description="Whether the section is approved")
    quality_score: Optional[int] = Field(default=None, description="Quality score 1-10 (AlphaStudent only): 10=super engaging, clear, all sources working")
    score_breakdown: Optional[Dict[str, int]] = Field(default=None, description="Detailed score breakdown by criteria")
    required_fixes: List[str] = Field(default_factory=list, description="Actionable revision requirements for WRITER")
    optional_suggestions: List[str] = Field(default_factory=list, description="Nice-to-have improvements")
    link_check_results: Optional[List[Dict[str, Any]]] = Field(default=None, description="URL validation results")
    direct_edits: List[DirectEdit] = Field(default_factory=list, description="Mechanical fixes EDITOR will apply directly")


class WebSearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    published: Optional[str] = None


class RunState(BaseModel):
    week_number: int = Field(description="Week being generated")
    sections: List[SectionSpec] = Field(description="All sections to generate")
    current_index: int = Field(default=0, description="Current section being processed")
    current_draft: Optional[SectionDraft] = Field(default=None, description="Section being worked on")
    education_review: Optional[ReviewNotes] = Field(default=None, description="Education expert feedback")
    alpha_review: Optional[ReviewNotes] = Field(default=None, description="Alpha student feedback")
    approved_sections: List[SectionDraft] = Field(default_factory=list, description="Completed sections")
    revision_count: int = Field(default=0, description="Number of revisions for current section")
    max_revisions: int = Field(default=1, description="Maximum revisions per section")
    context_summary: str = Field(default="", description="Summary of previously approved content")
    web_results: Optional[List[WebSearchResult]] = Field(default=None, description="Current web search results")
    context_usage: Optional[Dict[str, int]] = Field(default=None, description="Token usage tracking")
    optimization_context: Optional[Dict[str, Any]] = Field(default=None, description="Revision optimization context")
    final_coherence_review: Optional[Dict[str, Any]] = Field(default=None, description="ProgramDirector final review results")
    batch_revision_count: int = Field(default=0, description="Number of batch revision cycles completed")
    feedback_memory: List[str] = Field(default_factory=list, description="Accumulated feedback from all reviewers to avoid repeating mistakes")
    score_history: List[Dict[str, Any]] = Field(default_factory=list, description="History of scores for tracking progression")
    broken_links_details: List[Dict[str, Any]] = Field(default_factory=list, description="Detailed information about broken links for actionable feedback")
    failed_datasets_details: List[Dict[str, Any]] = Field(default_factory=list, description="Detailed information about failed datasets for actionable feedback")
    cached_template_guidelines: Optional[Dict[str, str]] = Field(default=None, description="Cached template and guidelines to avoid re-loading")


class LinkCheckResult(BaseModel):
    url: str
    ok: bool
    status: Optional[int] = None
    error: Optional[str] = None


class CourseInputs(BaseModel):
    week_number: int
    syllabus_path: str
    template_path: str
    guidelines_path: str
    sections_config_path: Optional[str] = None
    course_config_path: Optional[str] = None