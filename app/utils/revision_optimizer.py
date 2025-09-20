"""
Revision Loop Optimization System
Provides intelligent feedback prioritization and conflict resolution
"""

from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
from app.models.schemas import ReviewNotes


class FeedbackPriority(Enum):
    CRITICAL = 1    # Must fix - blocks approval
    HIGH = 2        # Important - affects quality significantly
    MEDIUM = 3      # Moderate - noticeable improvement
    LOW = 4         # Minor - nice to have


class FeedbackCategory(Enum):
    TEMPLATE_COMPLIANCE = "template"
    WLO_ALIGNMENT = "wlo"
    BUILDING_BLOCKS = "building_blocks"
    ACCESSIBILITY = "accessibility"
    CONTENT_QUALITY = "content"
    CITATIONS = "citations"
    CLARITY = "clarity"
    TECHNICAL = "technical"


@dataclass
class PrioritizedFeedback:
    """Structured feedback with priority and categorization"""
    issue: str
    priority: FeedbackPriority
    category: FeedbackCategory
    reviewer: str
    suggested_fix: Optional[str] = None
    examples: Optional[str] = None


class RevisionOptimizer:
    """Intelligent revision loop optimization"""

    # Keywords that indicate critical issues
    CRITICAL_KEYWORDS = [
        "missing", "required", "mandatory", "must", "critical",
        "template violation", "not compliant", "accessibility violation"
    ]

    # Keywords that indicate high priority issues
    HIGH_KEYWORDS = [
        "wlo", "learning objective", "assessment", "rubric",
        "building blocks", "citation", "reference"
    ]

    # Keywords that indicate medium priority issues
    MEDIUM_KEYWORDS = [
        "clarity", "structure", "organization", "flow",
        "example", "explanation", "formatting"
    ]

    def __init__(self):
        pass

    def optimize_feedback(
        self,
        education_review: ReviewNotes,
        alpha_review: ReviewNotes,
        revision_count: int,
        max_revisions: int
    ) -> Dict[str, Any]:
        """
        Optimize feedback for the current revision cycle
        Returns prioritized feedback and revision strategy
        """

        # Parse and prioritize all feedback
        prioritized_feedback = self._parse_and_prioritize_feedback(
            education_review, alpha_review
        )

        # Resolve conflicts between reviewers
        resolved_feedback = self._resolve_conflicts(prioritized_feedback)

        # Determine revision strategy based on revision count
        revision_strategy = self._determine_revision_strategy(
            resolved_feedback, revision_count, max_revisions
        )

        return {
            "prioritized_feedback": resolved_feedback,
            "revision_strategy": revision_strategy,
            "should_approve": revision_strategy["action"] == "approve",
            "focus_areas": revision_strategy["focus_areas"],
            "feedback_summary": self._create_feedback_summary(resolved_feedback)
        }

    def _parse_and_prioritize_feedback(
        self,
        education_review: ReviewNotes,
        alpha_review: ReviewNotes
    ) -> List[PrioritizedFeedback]:
        """Parse feedback from both reviewers and assign priorities"""

        all_feedback = []

        # Process EducationExpert feedback
        if not education_review.approved:
            for issue in education_review.required_fixes:
                priority = self._determine_priority(issue)
                category = self._determine_category(issue)

                all_feedback.append(PrioritizedFeedback(
                    issue=issue,
                    priority=priority,
                    category=category,
                    reviewer="EducationExpert",
                    suggested_fix=self._suggest_fix(issue, category)
                ))

        # Process AlphaStudent feedback
        if not alpha_review.approved:
            for issue in alpha_review.required_fixes:
                priority = self._determine_priority(issue)
                category = self._determine_category(issue)

                all_feedback.append(PrioritizedFeedback(
                    issue=issue,
                    priority=priority,
                    category=category,
                    reviewer="AlphaStudent",
                    suggested_fix=self._suggest_fix(issue, category)
                ))

        # Sort by priority (critical first)
        all_feedback.sort(key=lambda x: x.priority.value)

        return all_feedback

    def _determine_priority(self, issue: str) -> FeedbackPriority:
        """Determine priority level of an issue based on keywords"""
        issue_lower = issue.lower()

        # Check for critical keywords
        if any(keyword in issue_lower for keyword in self.CRITICAL_KEYWORDS):
            return FeedbackPriority.CRITICAL

        # Check for high priority keywords
        if any(keyword in issue_lower for keyword in self.HIGH_KEYWORDS):
            return FeedbackPriority.HIGH

        # Check for medium priority keywords
        if any(keyword in issue_lower for keyword in self.MEDIUM_KEYWORDS):
            return FeedbackPriority.MEDIUM

        # Default to low priority
        return FeedbackPriority.LOW

    def _determine_category(self, issue: str) -> FeedbackCategory:
        """Determine the category of an issue"""
        issue_lower = issue.lower()

        # Category mapping based on keywords
        category_keywords = {
            FeedbackCategory.TEMPLATE_COMPLIANCE: [
                "template", "structure", "heading", "format", "section"
            ],
            FeedbackCategory.WLO_ALIGNMENT: [
                "wlo", "learning objective", "objective", "alignment"
            ],
            FeedbackCategory.BUILDING_BLOCKS: [
                "figure", "table", "video", "multimedia", "annotation"
            ],
            FeedbackCategory.ACCESSIBILITY: [
                "alt text", "accessibility", "screen reader", "caption"
            ],
            FeedbackCategory.CONTENT_QUALITY: [
                "quality", "depth", "explanation", "example"
            ],
            FeedbackCategory.CITATIONS: [
                "citation", "reference", "source", "bibliography"
            ],
            FeedbackCategory.CLARITY: [
                "clarity", "clear", "confusing", "unclear", "understandable"
            ],
            FeedbackCategory.TECHNICAL: [
                "technical", "accuracy", "correct", "error"
            ]
        }

        for category, keywords in category_keywords.items():
            if any(keyword in issue_lower for keyword in keywords):
                return category

        return FeedbackCategory.CONTENT_QUALITY  # Default category

    def _suggest_fix(self, issue: str, category: FeedbackCategory) -> str:
        """Provide a suggested fix based on the issue and category"""

        fix_suggestions = {
            FeedbackCategory.TEMPLATE_COMPLIANCE:
                "Review the template requirements and ensure all required sections and headings are present.",

            FeedbackCategory.WLO_ALIGNMENT:
                "Explicitly state which WLO(s) this section addresses and ensure content directly supports the learning objective.",

            FeedbackCategory.BUILDING_BLOCKS:
                "Add proper captions, sources, and alt text for all multimedia elements following Building Blocks V2 requirements.",

            FeedbackCategory.ACCESSIBILITY:
                "Include descriptive alt text for all figures and tables, and ensure content is accessible to screen readers.",

            FeedbackCategory.CONTENT_QUALITY:
                "Enhance the content with more specific examples, clearer explanations, or deeper analysis.",

            FeedbackCategory.CITATIONS:
                "Add proper APA citations with URLs and dates, ensure all sources are referenced correctly.",

            FeedbackCategory.CLARITY:
                "Simplify language, break down complex concepts, and improve logical flow of ideas.",

            FeedbackCategory.TECHNICAL:
                "Verify technical accuracy, check calculations, and ensure all factual statements are correct."
        }

        return fix_suggestions.get(category, "Address the specific issue mentioned in the feedback.")

    def _resolve_conflicts(self, feedback_list: List[PrioritizedFeedback]) -> List[PrioritizedFeedback]:
        """Resolve conflicts between different reviewers"""

        # Group feedback by category
        by_category = {}
        for feedback in feedback_list:
            category = feedback.category
            if category not in by_category:
                by_category[category] = []
            by_category[category].append(feedback)

        resolved_feedback = []

        for category, category_feedback in by_category.items():
            if len(category_feedback) == 1:
                # No conflicts, add as-is
                resolved_feedback.extend(category_feedback)
            else:
                # Resolve conflicts within category
                resolved = self._resolve_category_conflicts(category_feedback)
                resolved_feedback.extend(resolved)

        return resolved_feedback

    def _resolve_category_conflicts(self, feedback_list: List[PrioritizedFeedback]) -> List[PrioritizedFeedback]:
        """Resolve conflicts within a specific category"""

        # Group by priority
        by_priority = {}
        for feedback in feedback_list:
            priority = feedback.priority
            if priority not in by_priority:
                by_priority[priority] = []
            by_priority[priority].append(feedback)

        resolved = []

        for priority, priority_feedback in by_priority.items():
            if len(priority_feedback) == 1:
                resolved.extend(priority_feedback)
            else:
                # Multiple reviewers with same priority - merge or choose most specific
                merged = self._merge_similar_feedback(priority_feedback)
                resolved.extend(merged)

        return resolved

    def _merge_similar_feedback(self, feedback_list: List[PrioritizedFeedback]) -> List[PrioritizedFeedback]:
        """Merge similar feedback from different reviewers"""

        if len(feedback_list) <= 1:
            return feedback_list

        # For now, prioritize EducationExpert feedback for structural issues
        # and AlphaStudent feedback for usability issues
        education_feedback = [f for f in feedback_list if f.reviewer == "EducationExpert"]
        alpha_feedback = [f for f in feedback_list if f.reviewer == "AlphaStudent"]

        # Prefer EducationExpert for template/compliance issues
        structural_categories = {
            FeedbackCategory.TEMPLATE_COMPLIANCE,
            FeedbackCategory.WLO_ALIGNMENT,
            FeedbackCategory.BUILDING_BLOCKS,
            FeedbackCategory.ACCESSIBILITY
        }

        # Prefer AlphaStudent for clarity/usability issues
        usability_categories = {
            FeedbackCategory.CLARITY,
            FeedbackCategory.CONTENT_QUALITY
        }

        merged = []

        if feedback_list[0].category in structural_categories and education_feedback:
            merged.extend(education_feedback)
        elif feedback_list[0].category in usability_categories and alpha_feedback:
            merged.extend(alpha_feedback)
        else:
            # Keep all feedback if no clear preference
            merged = feedback_list

        return merged

    def _determine_revision_strategy(
        self,
        feedback_list: List[PrioritizedFeedback],
        revision_count: int,
        max_revisions: int
    ) -> Dict[str, Any]:
        """Determine the revision strategy based on feedback and revision count"""

        # Count issues by priority
        priority_counts = {
            "CRITICAL": 0,
            "HIGH": 0,
            "MEDIUM": 0,
            "LOW": 0
        }

        for feedback in feedback_list:
            priority_counts[feedback.priority.name] += 1

        # Determine strategy
        if revision_count >= max_revisions:
            # Force approval - we've hit the limit
            return {
                "action": "approve",
                "reason": "Maximum revisions reached",
                "focus_areas": [],
                "priority_counts": priority_counts
            }

        # If no critical or high priority issues, approve
        if priority_counts["CRITICAL"] == 0 and priority_counts["HIGH"] == 0:
            return {
                "action": "approve",
                "reason": "No critical or high priority issues",
                "focus_areas": [],
                "priority_counts": priority_counts
            }

        # Determine focus areas for revision
        focus_areas = self._determine_focus_areas(feedback_list, revision_count, max_revisions)

        return {
            "action": "revise",
            "reason": f"{priority_counts['CRITICAL']} critical, {priority_counts['HIGH']} high priority issues",
            "focus_areas": focus_areas,
            "priority_counts": priority_counts
        }

    def _determine_focus_areas(
        self,
        feedback_list: List[PrioritizedFeedback],
        revision_count: int,
        max_revisions: int
    ) -> List[str]:
        """Determine which areas to focus on for this revision"""

        remaining_revisions = max_revisions - revision_count

        if remaining_revisions <= 1:
            # Last chance - focus only on critical issues
            focus_priorities = [FeedbackPriority.CRITICAL]
        elif remaining_revisions == 2:
            # Focus on critical and high priority
            focus_priorities = [FeedbackPriority.CRITICAL, FeedbackPriority.HIGH]
        else:
            # Early revisions - can address more issues
            focus_priorities = [FeedbackPriority.CRITICAL, FeedbackPriority.HIGH, FeedbackPriority.MEDIUM]

        focus_feedback = [f for f in feedback_list if f.priority in focus_priorities]

        # Group by category for focus areas
        categories = set(f.category.value for f in focus_feedback)

        return list(categories)

    def _create_feedback_summary(self, feedback_list: List[PrioritizedFeedback]) -> Dict[str, Any]:
        """Create a summary of feedback for logging and display"""

        total_issues = len(feedback_list)
        by_priority = {}
        by_category = {}
        by_reviewer = {}

        for feedback in feedback_list:
            # By priority
            priority = feedback.priority.name
            by_priority[priority] = by_priority.get(priority, 0) + 1

            # By category
            category = feedback.category.value
            by_category[category] = by_category.get(category, 0) + 1

            # By reviewer
            reviewer = feedback.reviewer
            by_reviewer[reviewer] = by_reviewer.get(reviewer, 0) + 1

        return {
            "total_issues": total_issues,
            "by_priority": by_priority,
            "by_category": by_category,
            "by_reviewer": by_reviewer,
            "top_issues": [f.issue for f in feedback_list[:3]]  # Top 3 most critical issues
        }


# Global instance
revision_optimizer = RevisionOptimizer()


def optimize_revision_cycle(
    education_review: ReviewNotes,
    alpha_review: ReviewNotes,
    revision_count: int,
    max_revisions: int = 3
) -> Dict[str, Any]:
    """Convenience function for optimizing revision cycles"""
    return revision_optimizer.optimize_feedback(
        education_review, alpha_review, revision_count, max_revisions
    )