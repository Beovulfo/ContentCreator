import os
import json
import re
from typing import Dict, List
from langchain_openai import AzureChatOpenAI, ChatOpenAI
from langchain.schema import HumanMessage, SystemMessage
from tenacity import retry, stop_after_attempt, wait_exponential

from app.models.schemas import RunState, SectionDraft, ReviewNotes
from app.agents.prompts import PromptTemplates
from app.tools import web, links
from app.utils.file_io import file_io
from app.utils.context_manager import ContextManager
from app.utils.error_handler import RobustWorkflowMixin, with_error_handling, ErrorSeverity
from app.utils.revision_optimizer import optimize_revision_cycle


class WorkflowNodes(RobustWorkflowMixin):
    """LangGraph workflow node implementations with robust error handling"""

    def __init__(self):
        # Initialize LLM clients for each agent
        # Check if Azure OpenAI is configured, otherwise use regular OpenAI
        if self._is_azure_configured():
            deployment = os.getenv("AZURE_DEPLOYMENT", "gpt-5-mini")
            self.program_director_llm = self._create_azure_llm(
                deployment=deployment,
                temperature=1.0,
                max_tokens=2000
            )
            self.content_expert_llm = self._create_azure_llm(
                deployment=deployment,
                temperature=1.0,
                max_tokens=4000
            )
            self.education_expert_llm = self._create_azure_llm(
                deployment=deployment,
                temperature=1.0,
                max_tokens=2000
            )
            self.alpha_student_llm = self._create_azure_llm(
                deployment=deployment,
                temperature=1.0,
                max_tokens=2000
            )
            # Initialize context managers with Azure model name
            self.content_expert_context = ContextManager(deployment)
            self.education_expert_context = ContextManager(deployment)
            self.alpha_student_context = ContextManager(deployment)
        else:
            # Fallback to regular OpenAI
            content_model = os.getenv("MODEL_CONTENT_EXPERT", "gpt-4o")
            self.program_director_llm = ChatOpenAI(
                model=os.getenv("MODEL_PROGRAM_DIRECTOR", "gpt-4o-mini"),
                temperature=1.0,
                max_tokens=2000
            )
            self.content_expert_llm = ChatOpenAI(
                model=content_model,
                temperature=1.0,
                max_tokens=4000
            )
            self.education_expert_llm = ChatOpenAI(
                model=os.getenv("MODEL_EDUCATION_EXPERT", "gpt-4o-mini"),
                temperature=1.0,
                max_tokens=2000
            )
            self.alpha_student_llm = ChatOpenAI(
                model=os.getenv("MODEL_ALPHA_STUDENT", "gpt-4o-mini"),
                temperature=1.0,
                max_tokens=2000
            )
            # Initialize context managers with OpenAI model names
            self.content_expert_context = ContextManager(content_model)
            self.education_expert_context = ContextManager(os.getenv("MODEL_EDUCATION_EXPERT", "gpt-4o-mini"))
            self.alpha_student_context = ContextManager(os.getenv("MODEL_ALPHA_STUDENT", "gpt-4o-mini"))

    def _is_azure_configured(self) -> bool:
        """Check if Azure OpenAI configuration is available"""
        required_vars = ["AZURE_ENDPOINT", "AZURE_SUBSCRIPTION_KEY", "AZURE_API_VERSION"]
        return all(os.getenv(var) for var in required_vars)

    def _create_azure_llm(self, deployment: str, temperature: float, max_tokens: int):
        """Create Azure OpenAI LLM instance"""
        return AzureChatOpenAI(
            azure_endpoint=os.getenv("AZURE_ENDPOINT"),
            azure_deployment=deployment,
            api_key=os.getenv("AZURE_SUBSCRIPTION_KEY"),
            api_version=os.getenv("AZURE_API_VERSION"),
            temperature=temperature,
            max_completion_tokens=max_tokens
        )

    def _extract_week_info(self, syllabus_content: str, week_number: int) -> str:
        """Extract only the relevant week information from syllabus"""
        lines = syllabus_content.split('\n')
        week_section = []
        capturing = False

        week_header = f"### Week {week_number}:"

        for line in lines:
            if line.startswith(week_header):
                capturing = True
                week_section.append(line)
            elif capturing and line.startswith("### Week ") and not line.startswith(week_header):
                # Found next week, stop capturing
                break
            elif capturing:
                week_section.append(line)

        return '\n'.join(week_section) if week_section else f"Week {week_number} information not found"

    def program_director_plan(self, state: RunState) -> RunState:
        """Initialize the workflow and plan section generation"""
        file_io.log_run_state(state.week_number, {
            "node": "program_director_plan",
            "action": "workflow_started",
            "total_sections": len(state.sections),
            "current_index": state.current_index
        })

        # Reset state for new run
        state.current_index = 0
        state.approved_sections = []
        state.context_summary = ""

        return state

    def program_director_request_section(self, state: RunState) -> RunState:
        """Request the next section from ContentExpert"""
        if state.current_index >= len(state.sections):
            return state  # All sections completed

        current_section = state.sections[state.current_index]

        # Load context from previously approved sections
        approved_section_ids = [s.section_id for s in state.approved_sections]
        previous_sections = file_io.load_approved_sections(approved_section_ids)

        # Build simple context summary
        if previous_sections:
            context_summary = f"Previous sections completed: {', '.join([s.split('/')[-1] for s in previous_sections])}"
        else:
            context_summary = "This is the first section."
        state.context_summary = context_summary

        file_io.log_run_state(state.week_number, {
            "node": "program_director_request_section",
            "section": current_section.id,
            "action": "section_requested",
            "previous_sections": len(approved_section_ids)
        })

        return state

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def content_expert_write(self, state: RunState) -> RunState:
        """ContentExpert (WRITER) creates/revises educational content based on Editor and Reviewer feedback"""
        current_section = state.sections[state.current_index]
        course_inputs = file_io.load_course_inputs(state.week_number)

        # ContentExpert ONLY gets:
        # 1. Section-specific instructions from ProgramDirector
        # 2. Week-specific syllabus content
        # 3. Guidelines.md for writing standards
        # 4. Previous section context (minimal)

        # Load syllabus AND guidelines for ContentExpert
        if course_inputs.syllabus_path.endswith('.md'):
            syllabus_content = self.safe_file_operation(
                lambda: file_io.read_markdown_file(course_inputs.syllabus_path),
                "read_syllabus_md"
            )
        else:
            syllabus_content = self.safe_file_operation(
                lambda: file_io.read_docx_file(course_inputs.syllabus_path),
                "read_syllabus_docx"
            )

        # Also load guidelines for content standards
        guidelines_content = self.safe_file_operation(
            lambda: file_io.read_markdown_file(course_inputs.guidelines_path),
            "read_guidelines"
        )

        # Extract only Week-specific information from syllabus
        week_info = self._extract_week_info(syllabus_content, state.week_number)

        # Optional web search for freshness (minimal)
        search_results = []
        needs_freshness = "latest" in current_section.description.lower() or "current" in current_section.description.lower()
        if needs_freshness:
            search_query = f"{current_section.title} data science latest 2024 2025"
            search_results = self.safe_web_search(
                lambda q: web.search(q, top_k=3),  # Reduced from 5 to 3
                search_query
            )

            file_io.log_run_state(state.week_number, {
                "node": "content_expert_write",
                "action": "web_search_no_results" if not search_results else "web_search_completed",
                "query": search_query
            })

        # Use simplified prompt method
        section_instruction = PromptTemplates.get_section_instruction(
            current_section.title,
            current_section.description,
            week_info
        )

        # Build revision context if this is a revision
        revision_context = ""
        if state.revision_count > 0:
            revision_context = "\n**REVISION FEEDBACK TO ADDRESS:**\n"
            if state.education_review and not state.education_review.approved:
                revision_context += f"EDITOR FEEDBACK (must address):\n"
                for fix in state.education_review.required_fixes:
                    revision_context += f"- {fix}\n"
            if state.alpha_review and not state.alpha_review.approved:
                revision_context += f"REVIEWER FEEDBACK (must address):\n"
                for fix in state.alpha_review.required_fixes:
                    revision_context += f"- {fix}\n"
            if hasattr(state, 'optimization_context') and state.optimization_context:
                revision_context += f"\nFOCUS AREAS for this revision: {', '.join(state.optimization_context['focus_areas'])}\n"

        # Add guidelines context for writing standards
        content_expert_prompt = f"""{section_instruction}

**Writing Guidelines (follow these standards):**
{guidelines_content[:800]}

**Previous Context:**
{state.context_summary}

**Web Search Results (if relevant):**
{json.dumps(search_results[:2], indent=2) if search_results else "No recent sources needed."}

{revision_context}

{"Write clear, educational content following the guidelines above." if state.revision_count == 0 else f"Revise the content below to address the feedback above while maintaining educational quality.{chr(10)}{chr(10)}**PREVIOUS DRAFT TO REVISE:**{chr(10)}{state.current_draft.content_md if state.current_draft else 'No previous draft found'}"}"""

        # Simple, direct LLM call - no complex context management
        file_io.log_run_state(state.week_number, {
            "node": "content_expert_write",
            "action": "context_prepared",
            "token_usage": {"original_total": len(content_expert_prompt), "truncated_total": len(content_expert_prompt), "truncation_applied": False, "system_tokens": 100, "total_tokens": len(content_expert_prompt) + 100, "limit": 123000},
            "context_truncated": False
        })

        # Simple LLM call with minimal context
        messages = [
            SystemMessage(content=PromptTemplates.get_content_expert_system()),
            HumanMessage(content=content_expert_prompt)
        ]

        response = self.safe_llm_call(
            self.content_expert_llm,
            messages,
            f"ContentExpert section {current_section.title}"
        )
        content_md = response.content

        # Extract links and create draft
        extracted_urls = links.extract_urls(content_md)
        word_count = len(content_md.split())

        draft = SectionDraft(
            section_id=current_section.id,
            content_md=content_md,
            links=extracted_urls,
            word_count=word_count,
            citations=self._extract_citations(content_md),
            wlo_mapping=self._extract_wlo_mapping(content_md)
        )

        state.current_draft = draft
        state.revision_count = 0  # Reset for new section

        file_io.log_run_state(state.week_number, {
            "node": "content_expert_write",
            "action": "draft_created",
            "word_count": word_count,
            "links_count": len(extracted_urls),
            "web_search_used": needs_freshness
        })

        return state

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def education_expert_review(self, state: RunState) -> RunState:
        """EducationExpert (EDITOR) reviews the draft for template compliance and educational standards"""
        if not state.current_draft:
            return state

        # Load template and guidelines for review
        course_inputs = file_io.load_course_inputs(state.week_number)
        template_content = file_io.read_docx_file(course_inputs.template_path)
        guidelines_content = file_io.read_markdown_file(course_inputs.guidelines_path)

        review_prompt = f"""
**Section to Review**: {state.current_draft.section_id}

**Draft Content**:
{state.current_draft.content_md}

**Template Requirements**:
{self._extract_template_constraints(template_content, state.current_draft.section_id)}

**Guidelines to Check**:
{guidelines_content[:1000]}...

Please review this draft against all template and guideline requirements.

Provide your assessment EXACTLY in this JSON format:
{{
  "approved": true/false,
  "required_fixes": ["fix 1", "fix 2"],
  "optional_suggestions": ["suggestion 1"]
}}

If approved is false, you MUST provide specific required_fixes.
"""

        messages = [
            SystemMessage(content=PromptTemplates.get_education_expert_system()),
            HumanMessage(content=review_prompt)
        ]

        response = self.safe_llm_call(
            self.education_expert_llm,
            messages,
            f"EducationExpert review for {state.current_draft.section_id}"
        )

        try:
            review_data = json.loads(response.content)
            review_notes = ReviewNotes(
                reviewer="education_expert",
                approved=review_data.get("approved", False),
                required_fixes=review_data.get("required_fixes", []),
                optional_suggestions=review_data.get("optional_suggestions", [])
            )
        except json.JSONDecodeError:
            # Fallback if JSON parsing fails
            review_notes = ReviewNotes(
                reviewer="education_expert",
                approved=False,
                required_fixes=["Unable to parse review - please revise content"],
                optional_suggestions=[]
            )

        state.education_review = review_notes

        file_io.log_run_state(state.week_number, {
            "node": "education_expert_review",
            "action": "review_completed",
            "approved": review_notes.approved,
            "fixes_required": len(review_notes.required_fixes)
        })

        return state

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def alpha_student_review(self, state: RunState) -> RunState:
        """AlphaStudent (REVIEWER) reviews the draft from a student learning perspective"""
        if not state.current_draft:
            return state

        # Safe link checking with fallback
        link_results = []
        if state.current_draft.links:
            link_results = self.safe_file_operation(
                lambda: links.check(state.current_draft.links),
                "check_links"
            ) or []  # Ensure we get a list even if None returned

            if link_results:
                file_io.log_run_state(state.week_number, {
                    "node": "alpha_student_review",
                    "action": "links_checked",
                    "total_links": len(state.current_draft.links),
                    "working_links": sum(1 for r in link_results if r.ok)
                })
            else:
                file_io.log_run_state(state.week_number, {
                    "node": "alpha_student_review",
                    "action": "link_check_skipped",
                    "reason": "Link checking failed or unavailable"
                })

        review_prompt = f"""
**Section to Review**: {state.current_draft.section_id}

**Draft Content**:
{state.current_draft.content_md}

**Link Check Results**:
{json.dumps([r.dict() for r in link_results], indent=2) if link_results else "No links to check"}

As an AlphaStudent, review this draft focusing on:
1. **Clarity**: Is the content clear and understandable for students?
2. **Relevance**: Does ALL content relate directly to the section topic? Flag any irrelevant information.
3. **Links**: Are all links working and appropriate?
4. **Learning**: Is this effective for student learning?

Provide your assessment EXACTLY in this JSON format:
{{
  "approved": true/false,
  "required_fixes": ["fix 1", "fix 2"],
  "optional_suggestions": ["suggestion 1"]
}}

If approved is false, you MUST provide specific required_fixes.
"""

        messages = [
            SystemMessage(content=PromptTemplates.get_alpha_student_system()),
            HumanMessage(content=review_prompt)
        ]

        response = self.safe_llm_call(
            self.alpha_student_llm,
            messages,
            f"AlphaStudent review for {state.current_draft.section_id}"
        )

        try:
            review_data = json.loads(response.content)
            review_notes = ReviewNotes(
                reviewer="alpha_student",
                approved=review_data.get("approved", False),
                required_fixes=review_data.get("required_fixes", []),
                optional_suggestions=review_data.get("optional_suggestions", []),
                link_check_results=[r.dict() for r in link_results]
            )
        except json.JSONDecodeError:
            review_notes = ReviewNotes(
                reviewer="alpha_student",
                approved=False,
                required_fixes=["Unable to parse review - please revise content"],
                optional_suggestions=[],
                link_check_results=[r.dict() for r in link_results]
            )

        state.alpha_review = review_notes

        file_io.log_run_state(state.week_number, {
            "node": "alpha_student_review",
            "action": "review_completed",
            "approved": review_notes.approved,
            "fixes_required": len(review_notes.required_fixes)
        })

        return state

    def program_director_merge_or_revise(self, state: RunState) -> RunState:
        """ProgramDirector decides whether to approve or request revision using intelligent optimization"""
        if not state.current_draft or not state.education_review or not state.alpha_review:
            return state

        # Use revision optimizer for intelligent decision making
        optimization_result = optimize_revision_cycle(
            state.education_review,
            state.alpha_review,
            state.revision_count,
            state.max_revisions
        )

        should_approve = optimization_result["should_approve"]
        revision_strategy = optimization_result["revision_strategy"]
        feedback_summary = optimization_result["feedback_summary"]

        # Log the optimization analysis
        file_io.log_run_state(state.week_number, {
            "node": "program_director_merge_or_revise",
            "action": "revision_analysis_completed",
            "section": state.current_draft.section_id,
            "revision_count": state.revision_count,
            "optimization_result": {
                "should_approve": should_approve,
                "strategy": revision_strategy,
                "feedback_summary": feedback_summary
            }
        })

        if should_approve:
            # Approve and save section
            file_path = file_io.save_section_draft(state.current_draft)
            state.approved_sections.append(state.current_draft)

            file_io.log_run_state(state.week_number, {
                "node": "program_director_merge_or_revise",
                "action": "section_approved",
                "section": state.current_draft.section_id,
                "saved_to": file_path,
                "approval_reason": revision_strategy["reason"],
                "total_issues_addressed": feedback_summary["total_issues"]
            })

            # Move to next section
            state.current_index += 1
            state.current_draft = None
            state.education_review = None
            state.alpha_review = None
            state.revision_count = 0

        else:
            # Request revision with focused feedback
            state.revision_count += 1

            # Store optimization context for the next revision
            state.optimization_context = {
                "focus_areas": revision_strategy["focus_areas"],
                "priority_feedback": [f.issue for f in optimization_result["prioritized_feedback"][:5]],
                "remaining_revisions": state.max_revisions - state.revision_count
            }

            file_io.log_run_state(state.week_number, {
                "node": "program_director_merge_or_revise",
                "action": "intelligent_revision_requested",
                "section": state.current_draft.section_id,
                "revision_count": state.revision_count,
                "focus_areas": revision_strategy["focus_areas"],
                "priority_issues": len([f for f in optimization_result["prioritized_feedback"]
                                      if f.priority.name in ["CRITICAL", "HIGH"]]),
                "strategy_reason": revision_strategy["reason"]
            })

        return state

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def program_director_final_review(self, state: RunState) -> RunState:
        """ProgramDirector (REVIEWER) performs final coherence review of complete weekly content"""
        if len(state.approved_sections) != len(state.sections):
            file_io.log_run_state(state.week_number, {
                "node": "program_director_final_review",
                "action": "error",
                "error": f"Cannot review incomplete week: {len(state.approved_sections)}/{len(state.sections)} sections"
            })
            return state

        # Load course inputs for WLO context
        course_inputs = file_io.load_course_inputs(state.week_number)
        syllabus_content = self.safe_file_operation(
            lambda: file_io.read_markdown_file(course_inputs.syllabus_path) if course_inputs.syllabus_path.endswith('.md')
                   else file_io.read_docx_file(course_inputs.syllabus_path),
            "read_syllabus_for_final_review"
        )
        week_info = self._extract_week_info(syllabus_content, state.week_number)

        # Compile all section content for review
        all_content = []
        for section in state.approved_sections:
            all_content.append(f"## {section.section_id}\n{section.content_md}\n")

        complete_week_content = "\n".join(all_content)

        # ProgramDirector reviews as REVIEWER for coherence
        final_review_prompt = f"""
**FINAL COHERENCE REVIEW - Week {state.week_number}**

**Weekly Learning Objectives (from syllabus):**
{week_info}

**Complete Weekly Content to Review:**
{complete_week_content}

**Your Task as REVIEWER:**
Evaluate this complete weekly content for overall coherence, quality, and effectiveness as a unified learning experience.

Provide your assessment EXACTLY in this JSON format:
{{
  "approved": true/false,
  "overall_quality_score": 1-10,
  "coherence_assessment": {{
    "logical_flow": "assessment of content progression",
    "wlo_coverage": "how well WLOs are addressed across all sections",
    "inter_section_connections": "how sections relate and build on each other",
    "academic_rigor": "appropriate for Master's level students",
    "student_experience": "clarity and engagement for learners"
  }},
  "required_fixes": ["any critical issues that must be addressed"],
  "optional_suggestions": ["improvements for next iteration"],
  "approval_reason": "why approving or what needs improvement"
}}

Focus on the COMPLETE weekly learning experience, not individual sections.
"""

        messages = [
            SystemMessage(content=PromptTemplates.get_program_director_final_review_system()),
            HumanMessage(content=final_review_prompt)
        ]

        response = self.safe_llm_call(
            self.program_director_llm,
            messages,
            f"ProgramDirector final review for Week {state.week_number}"
        )

        try:
            review_data = json.loads(response.content)
            state.final_coherence_review = {
                "approved": review_data.get("approved", False),
                "quality_score": review_data.get("overall_quality_score", 0),
                "coherence_assessment": review_data.get("coherence_assessment", {}),
                "required_fixes": review_data.get("required_fixes", []),
                "optional_suggestions": review_data.get("optional_suggestions", []),
                "approval_reason": review_data.get("approval_reason", "No reason provided")
            }
        except json.JSONDecodeError:
            # Fallback if JSON parsing fails
            state.final_coherence_review = {
                "approved": False,
                "quality_score": 0,
                "coherence_assessment": {},
                "required_fixes": ["ProgramDirector review failed - unable to parse assessment"],
                "optional_suggestions": [],
                "approval_reason": "Technical error in review process"
            }

        file_io.log_run_state(state.week_number, {
            "node": "program_director_final_review",
            "action": "final_review_completed",
            "approved": state.final_coherence_review["approved"],
            "quality_score": state.final_coherence_review["quality_score"],
            "fixes_required": len(state.final_coherence_review["required_fixes"])
        })

        return state

    def program_director_finalize_week(self, state: RunState) -> RunState:
        """Compile final weekly content file after ProgramDirector approval"""
        # Check if ProgramDirector has approved the content
        if not hasattr(state, 'final_coherence_review') or not state.final_coherence_review.get("approved", False):
            file_io.log_run_state(state.week_number, {
                "node": "program_director_finalize_week",
                "action": "error",
                "error": "Cannot finalize week without ProgramDirector final approval"
            })
            return state

        if len(state.approved_sections) != len(state.sections):
            file_io.log_run_state(state.week_number, {
                "node": "program_director_finalize_week",
                "action": "error",
                "error": f"Expected {len(state.sections)} sections, got {len(state.approved_sections)}"
            })
            return state

        # Generate week title
        week_title = f"Data Science Week {state.week_number}"

        # Compile final document
        final_path = file_io.compile_weekly_content(
            state.week_number,
            state.approved_sections,
            week_title
        )

        file_io.log_run_state(state.week_number, {
            "node": "program_director_finalize_week",
            "action": "week_completed",
            "final_path": final_path,
            "total_sections": len(state.approved_sections),
            "total_word_count": sum(s.word_count for s in state.approved_sections),
            "final_quality_score": state.final_coherence_review.get("quality_score", "N/A"),
            "program_director_approved": True
        })

        return state

    # Helper methods

    def _extract_wlos_from_syllabus(self, syllabus_content: str, week_number: int) -> str:
        """Extract Weekly Learning Objectives from syllabus"""
        # This is a simplified extraction - in practice you'd parse the DOCX more carefully
        lines = syllabus_content.split('\n')
        wlo_section = []
        in_week_section = False

        for line in lines:
            if f"week {week_number}" in line.lower() or f"week{week_number}" in line.lower():
                in_week_section = True
                continue
            if in_week_section and ("week " in line.lower() and str(week_number + 1) in line):
                break
            if in_week_section:
                wlo_section.append(line)

        return '\n'.join(wlo_section[:10])  # Limit to prevent token overflow

    def _extract_template_constraints(self, template_content: str, section_id: str) -> str:
        """Extract relevant template constraints for the section"""
        # This is simplified - in practice you'd parse DOCX structure more carefully
        return f"Follow standard academic format with clear headings and subheadings for {section_id}"

    def _extract_citations(self, content_md: str) -> List[str]:
        """Extract bibliography entries from markdown content"""
        citations = []
        lines = content_md.split('\n')

        for line in lines:
            line = line.strip()
            # Look for reference-style lines
            if (line.startswith('*') or line.startswith('-') or line.startswith('1.')) and \
               ('http' in line or '(' in line and ')' in line):
                citations.append(line)

        return citations

    def _extract_wlo_mapping(self, content_md: str) -> Dict[str, str]:
        """Extract WLO mapping notes from content"""
        # Look for explicit WLO references in the content
        wlo_pattern = r'WLO[- ]?(\d+)'
        matches = re.findall(wlo_pattern, content_md, re.IGNORECASE)

        mapping = {}
        for match in matches:
            mapping[f"WLO{match}"] = "Referenced in content"

        return mapping