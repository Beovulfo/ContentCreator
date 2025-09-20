import os
import json
import re
from typing import Dict, List
from langchain_openai import AzureChatOpenAI, ChatOpenAI
from langchain.schema import HumanMessage, SystemMessage
from tenacity import retry, stop_after_attempt, wait_exponential

from app.models.schemas import RunState, SectionDraft, ReviewNotes
from app.agents.prompts import PromptTemplates, PromptBuilder
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
                temperature=0.3,
                max_tokens=2000
            )
            self.content_expert_llm = self._create_azure_llm(
                deployment=deployment,
                temperature=0.7,
                max_tokens=4000
            )
            self.education_expert_llm = self._create_azure_llm(
                deployment=deployment,
                temperature=0.3,
                max_tokens=2000
            )
            self.alpha_student_llm = self._create_azure_llm(
                deployment=deployment,
                temperature=0.5,
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
                temperature=0.3,
                max_tokens=2000
            )
            self.content_expert_llm = ChatOpenAI(
                model=content_model,
                temperature=0.7,
                max_tokens=4000
            )
            self.education_expert_llm = ChatOpenAI(
                model=os.getenv("MODEL_EDUCATION_EXPERT", "gpt-4o-mini"),
                temperature=0.3,
                max_tokens=2000
            )
            self.alpha_student_llm = ChatOpenAI(
                model=os.getenv("MODEL_ALPHA_STUDENT", "gpt-4o-mini"),
                temperature=0.5,
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
            max_tokens=max_tokens
        )

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

        # Build context summary
        context_summary = PromptBuilder.build_section_context(previous_sections)
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
        """ContentExpert writes the section draft"""
        current_section = state.sections[state.current_index]

        # Load course materials with error handling
        course_inputs = file_io.load_course_inputs(state.week_number)

        # Safe file loading with fallbacks
        syllabus_content = self.safe_file_operation(
            lambda: file_io.read_docx_file(course_inputs.syllabus_path),
            "read_syllabus_docx"
        )
        template_content = self.safe_file_operation(
            lambda: file_io.read_docx_file(course_inputs.template_path),
            "read_template_docx"
        )
        guidelines_content = self.safe_file_operation(
            lambda: file_io.read_markdown_file(course_inputs.guidelines_path),
            "read_guidelines_md"
        )

        # Check if freshness is needed
        needs_freshness = PromptBuilder.check_freshness_needed(
            current_section.description,
            syllabus_content
        )

        # Safe web search with fallback
        search_results = []
        if needs_freshness:
            search_query = f"{current_section.title} data science latest 2024 2025"
            search_results = self.safe_web_search(
                lambda q: web.search(q, top_k=5),
                search_query
            )

            if search_results:
                file_io.log_run_state(state.week_number, {
                    "node": "content_expert_write",
                    "action": "web_search_completed",
                    "results_count": len(search_results)
                })
            else:
                file_io.log_run_state(state.week_number, {
                    "node": "content_expert_write",
                    "action": "web_search_no_results",
                    "query": search_query
                })

        # Build the prompt for ContentExpert
        key_guidelines = PromptBuilder.extract_key_guidelines(guidelines_content, current_section.id)

        request_prompt = PromptTemplates.get_program_director_request(
            section_title=current_section.title,
            section_id=current_section.id,
            section_description=current_section.description,
            section_ordinal=current_section.ordinal,
            total_sections=len(state.sections),
            wlos=self._extract_wlos_from_syllabus(syllabus_content, state.week_number),
            template_constraints=self._extract_template_constraints(template_content, current_section.id),
            previous_context=state.context_summary,
            key_guidelines=key_guidelines
        )

        # Prepare context with intelligent truncation
        system_prompt = PromptTemplates.get_content_expert_system()

        # Load previous sections for context
        approved_section_ids = [s.section_id for s in state.approved_sections]
        previous_sections = file_io.load_approved_sections(approved_section_ids)

        # Prepare web results for context manager
        web_results_list = []
        if needs_freshness and search_results:
            # Convert search results to dictionaries for context manager
            web_results_list = [
                {
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                    "published": result.published
                }
                for result in search_results
            ]
            # Store in state for future use
            state.web_results = search_results

        # Use context manager to prepare optimized content
        final_system_prompt, final_user_prompt, token_usage = self.content_expert_context.prepare_context(
            system_prompt=system_prompt,
            user_content=request_prompt,
            previous_sections=previous_sections,
            web_results=web_results_list,
            syllabus_content=syllabus_content,
            template_content=template_content,
            guidelines_content=guidelines_content
        )

        # Store context usage in state
        state.context_usage = token_usage

        # Log context management info
        file_io.log_run_state(state.week_number, {
            "node": "content_expert_write",
            "action": "context_prepared",
            "token_usage": token_usage,
            "context_truncated": token_usage.get("truncation_applied", False)
        })

        # Safe LLM call with error handling
        messages = [
            SystemMessage(content=final_system_prompt),
            HumanMessage(content=final_user_prompt)
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
        """EducationExpert reviews the draft"""
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

Please review this draft against all template and guideline requirements. Provide your assessment in JSON format.
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
        """AlphaStudent reviews the draft"""
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

Please review this draft from a student's perspective. Focus on clarity, usability, and learning effectiveness. Provide your assessment in JSON format.
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

    def program_director_finalize_week(self, state: RunState) -> RunState:
        """Compile final weekly content file"""
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
            "total_word_count": sum(s.word_count for s in state.approved_sections)
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