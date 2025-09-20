import os
import json
import re
import time
from typing import Dict, List
from langchain_openai import AzureChatOpenAI, ChatOpenAI
from langchain.schema import HumanMessage, SystemMessage
from tenacity import retry, stop_after_attempt, wait_exponential

from app.models.schemas import RunState, SectionDraft, ReviewNotes
from app.agents.prompts import PromptTemplates
from app.tools import links
from app.tools.web import get_web_tool
from app.utils.file_io import file_io
from app.utils.context_manager import ContextManager
from app.utils.error_handler import RobustWorkflowMixin, with_error_handling, ErrorSeverity
from app.utils.revision_optimizer import optimize_revision_cycle
from app.utils.tracer import get_tracer


class WorkflowNodes(RobustWorkflowMixin):
    """LangGraph workflow node implementations with autonomous W/E/R architecture"""

    def __init__(self):
        # Initialize LLM clients for Writer/Editor/Reviewer agents only
        if self._is_azure_configured():
            deployment = os.getenv("AZURE_DEPLOYMENT", "gpt-5-mini")
            self.content_expert_llm = self._create_azure_llm(
                deployment=deployment,
                temperature=1.0,
                max_completion_tokens=4000
            )
            self.education_expert_llm = self._create_azure_llm(
                deployment=deployment,
                temperature=1.0,
                max_completion_tokens=2000
            )
            self.alpha_student_llm = self._create_azure_llm(
                deployment=deployment,
                temperature=1.0,
                max_completion_tokens=2000
            )
            # Initialize context managers with Azure model name
            self.content_expert_context = ContextManager(deployment)
            self.education_expert_context = ContextManager(deployment)
            self.alpha_student_context = ContextManager(deployment)
        else:
            # Fallback to regular OpenAI
            content_model = os.getenv("MODEL_CONTENT_EXPERT", "gpt-4o")
            self.content_expert_llm = ChatOpenAI(
                model=content_model,
                temperature=1.0,
                max_completion_tokens=4000
            )
            self.education_expert_llm = ChatOpenAI(
                model=os.getenv("MODEL_EDUCATION_EXPERT", "gpt-4o-mini"),
                temperature=1.0,
                max_completion_tokens=2000
            )
            self.alpha_student_llm = ChatOpenAI(
                model=os.getenv("MODEL_ALPHA_STUDENT", "gpt-4o-mini"),
                temperature=1.0,
                max_completion_tokens=2000
            )
            # Initialize context managers with OpenAI model names
            self.content_expert_context = ContextManager(content_model)
            self.education_expert_context = ContextManager(os.getenv("MODEL_EDUCATION_EXPERT", "gpt-4o-mini"))
            self.alpha_student_context = ContextManager(os.getenv("MODEL_ALPHA_STUDENT", "gpt-4o-mini"))

    def _is_azure_configured(self) -> bool:
        """Check if Azure OpenAI configuration is available"""
        required_vars = ["AZURE_ENDPOINT", "AZURE_SUBSCRIPTION_KEY", "AZURE_API_VERSION"]
        return all(os.getenv(var) for var in required_vars)

    def _create_azure_llm(self, deployment: str, temperature: float, max_completion_tokens: int):
        """Create Azure OpenAI LLM instance (gpt-5-mini only supports default parameters)"""
        # gpt-5-mini only supports temperature=1.0 and no max_completion_tokens
        return AzureChatOpenAI(
            azure_endpoint=os.getenv("AZURE_ENDPOINT"),
            azure_deployment=deployment,
            api_key=os.getenv("AZURE_SUBSCRIPTION_KEY"),
            api_version=os.getenv("AZURE_API_VERSION")
            # Note: gpt-5-mini doesn't support custom temperature or max_completion_tokens
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

    # =============================================================================
    # AUTONOMOUS W/E/R WORKFLOW NODES
    # =============================================================================

    def initialize_workflow(self, state: RunState) -> RunState:
        """Initialize the autonomous workflow - no interactive validation"""
        tracer = get_tracer()
        if tracer:
            tracer.trace_node_start("initialize_workflow")

        print(f"🎓 Initializing autonomous content generation for Week {state.week_number}")
        print(f"📋 Generating {len(state.sections)} sections using W/E/R architecture")

        # Reset state for clean start
        state.current_index = 0
        state.approved_sections = []
        state.context_summary = ""

        file_io.log_run_state(state.week_number, {
            "node": "initialize_workflow",
            "action": "workflow_started",
            "total_sections": len(state.sections),
            "architecture": "Writer/Editor/Reviewer"
        })

        if tracer:
            tracer.trace_node_complete("initialize_workflow")
        return state

    def request_next_section(self, state: RunState) -> RunState:
        """Request the next section to be written"""
        tracer = get_tracer()
        if tracer:
            tracer.trace_node_start("request_next_section")

        # Check if we have more sections to process
        if state.current_index >= len(state.sections):
            if tracer:
                tracer.trace_node_complete("request_next_section")
            return state  # No more sections

        current_section = state.sections[state.current_index]

        print(f"[{state.current_index + 1}/{len(state.sections)}] 📝 Section: {current_section.title}")

        # Reset section-specific state
        state.current_draft = None
        state.education_review = None
        state.alpha_review = None
        state.revision_count = 0

        file_io.log_run_state(state.week_number, {
            "node": "request_next_section",
            "section": current_section.id,
            "action": "section_requested",
            "progress": f"{state.current_index + 1}/{len(state.sections)}"
        })

        if tracer:
            tracer.trace_node_complete("request_next_section")
        return state

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def content_expert_write(self, state: RunState) -> RunState:
        """ContentExpert (WRITER) creates section content"""
        tracer = get_tracer()
        if tracer:
            tracer.trace_node_start("content_expert_write", {"revision_count": state.revision_count})

        current_section = state.sections[state.current_index]

        if state.revision_count > 0:
            print(f"✏️  ContentExpert revising section (attempt {state.revision_count + 1}/{state.max_revisions + 1})")
        else:
            print(f"✍️  ContentExpert writing: {current_section.title}")

        # Load course materials
        course_inputs = file_io.load_course_inputs(state.week_number)

        # Load syllabus content
        syllabus_content = self.safe_file_operation(
            lambda: file_io.read_markdown_file(course_inputs.syllabus_path) if course_inputs.syllabus_path.endswith('.md')
                   else file_io.read_docx_file(course_inputs.syllabus_path),
            "read_syllabus_for_content_expert"
        )

        # Load guidelines
        guidelines_content = self.safe_file_operation(
            lambda: file_io.read_markdown_file(course_inputs.guidelines_path),
            "read_guidelines_for_content_expert"
        )

        # Extract week-specific information
        week_info = self._extract_week_info(syllabus_content, state.week_number)

        # Check if we need fresh web content
        web_tool = get_web_tool()
        search_results = self.safe_web_search(
            lambda q: web_tool.search(q, top_k=5),
            f"{current_section.title} data science {state.week_number} latest"
        )

        # Build detailed section instruction
        section_instruction = PromptTemplates.get_section_instruction(
            current_section.title,
            current_section.description,
            week_info,
            current_section.constraints
        )

        # Add revision feedback if this is a revision
        revision_feedback = ""
        if state.education_review and not state.education_review.approved:
            revision_feedback += f"\n**EDITOR FEEDBACK TO ADDRESS:**\n"
            for fix in state.education_review.required_fixes:
                revision_feedback += f"• {fix}\n"

        if state.alpha_review and not state.alpha_review.approved:
            revision_feedback += f"\n**REVIEWER FEEDBACK TO ADDRESS:**\n"
            for fix in state.alpha_review.required_fixes:
                revision_feedback += f"• {fix}\n"

        # For now, skip outline generation to isolate the issue
        outline_content = f"""
        Section outline for {current_section.title}:
        1. Introduce the key concepts with engaging narrative
        2. Provide concrete examples and applications
        3. Connect to learning objectives
        4. Include relevant citations and references
        5. Use flowing prose style throughout
        """

        # Write content directly
        content_prompt = f"""
{section_instruction}

**Your Detailed Outline:**
{outline_content}

**COMPLETE AUTHORING GUIDELINES (READ THOROUGHLY):**
{guidelines_content}

{revision_feedback}

**Available Web Research:**
{json.dumps([result.model_dump() for result in search_results[:3]], indent=2) if search_results else "No recent search results available"}

**Context from Previous Sections:**
{state.context_summary[:400] if state.context_summary else "This is the first section"}

Now write the complete section content following your outline.

CRITICAL REQUIREMENTS:
- Write in clear, engaging NARRATIVE PROSE (not bullet points or lists)
- Tell a story that teaches the concepts progressively
- Include concrete examples and practical applications
- Integrate citations naturally within the narrative flow
- Use proper markdown formatting with clear section headers (H2, H3)
- Ensure content is educational and student-friendly at Master's level
- Focus on comprehension and learning, not just information delivery

Create the complete section content now.
"""

        content_messages = [
            SystemMessage(content=PromptTemplates.get_content_expert_system()),
            HumanMessage(content=content_prompt)
        ]

        # Make the LLM call for content generation
        response = self.safe_llm_call(
            self.content_expert_llm,
            content_messages,
            context_info=f"content_expert_write_{current_section.id}"
        )

        # Extract content and create draft
        if not response:
            content_md = "Content generation temporarily unavailable. Please try again later."
        else:
            content_md = response.content if hasattr(response, 'content') else str(response)

        # Extract metadata from the content
        extracted_urls = self.safe_file_operation(
            lambda: links.extract_urls(content_md),
            "extract_urls_from_content"
        )
        if not extracted_urls:
            extracted_urls = []
        word_count = len(content_md.split())

        # Create the section draft
        try:
            citations = self._extract_citations(content_md)
            wlo_mapping = self._extract_wlo_mapping(content_md)

            draft = SectionDraft(
                section_id=current_section.id,
                content_md=content_md,
                links=extracted_urls,
                word_count=word_count,
                citations=citations,
                wlo_mapping=wlo_mapping
            )
        except Exception as e:
            print(f"❌ Error creating SectionDraft: {str(e)}")
            # Create minimal draft with fallback values
            draft = SectionDraft(
                section_id=current_section.id,
                content_md=content_md,
                links=extracted_urls or [],
                word_count=word_count,
                citations=[],
                wlo_mapping={}
            )

        state.current_draft = draft

        # Update context for next sections
        if len(state.approved_sections) < len(state.sections):
            summary_parts = [f"Section {current_section.id}: {current_section.title} - {word_count} words"]
            state.context_summary = "; ".join(summary_parts)

        print(f"   📝 Generated {word_count} words")

        file_io.log_run_state(state.week_number, {
            "node": "content_expert_write",
            "section": current_section.id,
            "action": "draft_created",
            "word_count": word_count,
            "revision_count": state.revision_count
        })

        if tracer:
            tracer.trace_node_complete("content_expert_write")
        return state

    def education_expert_review(self, state: RunState) -> RunState:
        """EducationExpert (EDITOR) reviews and provides feedback"""
        tracer = get_tracer()
        if tracer:
            tracer.trace_node_start("education_expert_review")

        print(f"📋 EducationExpert reviewing for compliance and structure")

        current_section = state.sections[state.current_index]

        # Load template and guidelines for review
        course_inputs = file_io.load_course_inputs(state.week_number)
        template_content = self.safe_file_operation(
            lambda: file_io.read_markdown_file(course_inputs.template_path),
            "read_template_for_review"
        )
        guidelines_content = self.safe_file_operation(
            lambda: file_io.read_markdown_file(course_inputs.guidelines_path),
            "read_guidelines_for_review"
        )

        # Build review prompt with full guidelines
        education_review_prompt = f"""
**SECTION TO REVIEW:**
{state.current_draft.content_md}

**COMPLETE EDITORIAL GUIDELINES (ENFORCE ALL):**
{guidelines_content}

**TEMPLATE REQUIREMENTS:**
{self._extract_template_constraints(template_content, current_section.id)}

**SECTION SPECIFICATION:**
- Title: {current_section.title}
- Description: {current_section.description}
- Constraints: {json.dumps(current_section.constraints, indent=2)}

Review this section thoroughly as the EDITOR. Your job is to enforce ALL guidelines strictly.

CRITICAL REVIEW FOCUS:
1. NARRATIVE PROSE: Is content written in flowing paragraphs? Reject if bullet points/lists are used.
2. GUIDELINE COMPLIANCE: Does content meet ALL authoring guidelines exactly?
3. EDUCATIONAL QUALITY: Does content effectively teach at Master's level?
4. CITATION INTEGRATION: Are citations properly integrated in APA format?
5. WLO ALIGNMENT: Is connection to learning objectives explicit?

Return a JSON object with:
{{
  "approved": boolean,
  "required_fixes": ["specific fix 1", "specific fix 2"],
  "optional_suggestions": ["suggestion 1", "suggestion 2"]
}}

Be thorough and demanding. Only approve content that meets ALL standards.
"""

        messages = [
            SystemMessage(content=PromptTemplates.get_education_expert_system()),
            HumanMessage(content=education_review_prompt)
        ]

        # Make the LLM call with error handling
        response = self.safe_llm_call(
            self.education_expert_llm,
            messages,
            context_info=f"education_expert_review_{current_section.id}"
        )

        # Parse the review response
        try:
            review_content = response.content if hasattr(response, 'content') else str(response)
            review_data = json.loads(review_content)

            state.education_review = ReviewNotes(
                reviewer="EducationExpert",
                approved=review_data.get("approved", True),
                required_fixes=review_data.get("required_fixes", []),
                optional_suggestions=review_data.get("optional_suggestions", [])
            )
        except json.JSONDecodeError:
            # Fallback if JSON parsing fails
            state.education_review = ReviewNotes(
                reviewer="EducationExpert",
                approved=True,
                required_fixes=[],
                optional_suggestions=["Review parsing failed - using fallback approval"]
            )

        approval_status = "✅ approved" if state.education_review.approved else "❌ revision needed"
        print(f"   📋 EducationExpert: {approval_status}")

        file_io.log_run_state(state.week_number, {
            "node": "education_expert_review",
            "section": current_section.id,
            "approved": state.education_review.approved,
            "fixes_required": len(state.education_review.required_fixes)
        })

        if tracer:
            tracer.trace_node_complete("education_expert_review")
        return state

    def alpha_student_review(self, state: RunState) -> RunState:
        """AlphaStudent (REVIEWER) reviews from student perspective"""
        tracer = get_tracer()
        if tracer:
            tracer.trace_node_start("alpha_student_review")

        print(f"🎓 AlphaStudent reviewing for clarity and usability")

        current_section = state.sections[state.current_index]

        # Check links if any exist
        link_results = self.safe_file_operation(
            lambda: links.check(state.current_draft.links if state.current_draft else []),
            "check_links_for_alpha_review"
        )

        # Count working/broken links
        working_links = 0
        broken_links = 0

        if link_results:
            for r in link_results:
                if hasattr(r, 'ok') and r.ok:
                    working_links += 1
                else:
                    broken_links += 1

        link_summary = f"{working_links} working, {broken_links} broken" if link_results else "no links"

        # Get current section details for learning context
        current_section = state.sections[state.current_index]

        # Build review prompt focused on learning quality
        alpha_review_prompt = f"""
**SECTION TO REVIEW (Week {state.week_number} Data Science Content):**
{state.current_draft.content_md}

**SECTION CONTEXT:**
- Title: {current_section.title}
- Topic: Week {state.week_number} Data Science concepts
- Learning Focus: {current_section.description}

**LINK CHECK RESULTS:**
{json.dumps([{
    "url": getattr(r, 'url', ''),
    "status": "working" if getattr(r, 'ok', False) else "broken",
    "error": getattr(r, 'error', None)
} for r in (link_results or [])], indent=2)}

**YOUR LEARNING-FOCUSED REVIEW TASK:**
Read this content as a Master's student genuinely trying to learn about this week's data science topic.

CRITICAL QUESTIONS TO ANSWER:
1. Does this content actually TEACH me about data science concepts through narrative?
2. Can I understand the concepts progressively through the story being told?
3. Are examples integrated naturally to help me understand, not just listed?
4. Would I feel more knowledgeable about data science after reading this?
5. Does the narrative flow help me see WHY these concepts matter?
6. Is this content engaging and does it inspire further learning?

Focus on whether this content serves genuine learning needs, not just information delivery.

Return a JSON object:
{{
  "approved": boolean,
  "required_fixes": ["learning issue 1", "learning issue 2"],
  "optional_suggestions": ["learning improvement 1", "learning improvement 2"]
}}

Be honest about whether this content effectively teaches data science concepts.
"""

        messages = [
            SystemMessage(content=PromptTemplates.get_alpha_student_system()),
            HumanMessage(content=alpha_review_prompt)
        ]

        # Make the LLM call with error handling
        response = self.safe_llm_call(
            self.alpha_student_llm,
            messages,
            context_info=f"alpha_student_review_{current_section.id}"
        )

        # Parse the review response
        try:
            review_content = response.content if hasattr(response, 'content') else str(response)
            review_data = json.loads(review_content)

            # Add link check results to review
            link_check_results = []
            if link_results:
                for r in link_results:
                    link_check_results.append({
                        "url": getattr(r, 'url', ''),
                        "ok": getattr(r, 'ok', False),
                        "status": getattr(r, 'status', 'unknown'),
                        "error": getattr(r, 'error', None)
                    })

            state.alpha_review = ReviewNotes(
                reviewer="AlphaStudent",
                approved=review_data.get("approved", True),
                required_fixes=review_data.get("required_fixes", []),
                optional_suggestions=review_data.get("optional_suggestions", []),
                link_check_results=link_check_results
            )
        except json.JSONDecodeError:
            # Fallback if JSON parsing fails - create link_check_results for fallback too
            link_check_results = []
            if link_results:
                for r in link_results:
                    link_check_results.append({
                        "url": getattr(r, 'url', ''),
                        "ok": getattr(r, 'ok', False),
                        "status": getattr(r, 'status', 'unknown'),
                        "error": getattr(r, 'error', None)
                    })

            state.alpha_review = ReviewNotes(
                reviewer="AlphaStudent",
                approved=True,
                required_fixes=[],
                optional_suggestions=["Review parsing failed - using fallback approval"],
                link_check_results=link_check_results
            )

        approval_status = "✅ approved" if state.alpha_review.approved else "❌ revision needed"
        print(f"   🎓 AlphaStudent: {approval_status} (links: {link_summary})")

        file_io.log_run_state(state.week_number, {
            "node": "alpha_student_review",
            "section": current_section.id,
            "approved": state.alpha_review.approved,
            "fixes_required": len(state.alpha_review.required_fixes),
            "working_links": working_links,
            "broken_links": broken_links
        })

        if tracer:
            tracer.trace_node_complete("alpha_student_review")
        return state

    def merge_section_or_revise(self, state: RunState) -> RunState:
        """Autonomous decision: approve and move on, or revise current section"""
        tracer = get_tracer()
        if tracer:
            tracer.trace_node_start("merge_section_or_revise")

        current_section = state.sections[state.current_index]

        # Check review results
        education_approved = state.education_review and state.education_review.approved
        alpha_approved = state.alpha_review and state.alpha_review.approved
        both_approved = education_approved and alpha_approved
        max_revisions_reached = state.revision_count >= state.max_revisions
        minimum_iterations_completed = state.revision_count >= 1  # Reduce to 1 minimum iteration for performance

        # Only approve if both reviewers approve AND we've done minimum iterations
        if both_approved and minimum_iterations_completed:
            # Approve and save section
            print(f"✅ Section approved - saving to temporal output")

            # Save the approved section
            file_path = file_io.save_section_draft(state.current_draft, backup=True)
            state.approved_sections.append(state.current_draft)

            # Move to next section
            state.current_index += 1
            state.revision_count = 0

            reason = f"both reviewers approved after {state.revision_count + 1} iterations"
            print(f"   💾 Saved to: {file_path}")
            print(f"   📊 Progress: {len(state.approved_sections)}/{len(state.sections)} sections complete")

            file_io.log_run_state(state.week_number, {
                "node": "merge_section_or_revise",
                "action": "section_approved",
                "section": current_section.id,
                "reason": reason,
                "word_count": state.current_draft.word_count if state.current_draft else 0,
                "progress": f"{len(state.approved_sections)}/{len(state.sections)}"
            })
        elif max_revisions_reached and not minimum_iterations_completed:
            # Force approval if max revisions reached even without minimum iterations
            print(f"⚠️ Maximum revisions reached - approving section with current quality")

            # Save the section as-is
            file_path = file_io.save_section_draft(state.current_draft, backup=True)
            state.approved_sections.append(state.current_draft)

            # Move to next section
            state.current_index += 1
            state.revision_count = 0

            print(f"   💾 Saved to: {file_path}")
            print(f"   📊 Progress: {len(state.approved_sections)}/{len(state.sections)} sections complete")

        else:
            # Revision needed
            if not minimum_iterations_completed:
                print(f"🔄 Revision needed - minimum {1} iterations required (current: {state.revision_count})")
            elif not both_approved:
                print(f"🔄 Revision needed - reviewers require changes")

            state.revision_count += 1
            issues = []
            if not education_approved:
                issues.extend([f"Editor: {fix}" for fix in state.education_review.required_fixes])
            if not alpha_approved:
                issues.extend([f"Reviewer: {fix}" for fix in state.alpha_review.required_fixes])

            print(f"   📝 Issues to address ({len(issues)}):")
            for issue in issues[:3]:  # Show first 3 issues
                print(f"      • {issue}")
            if len(issues) > 3:
                print(f"      • ... and {len(issues) - 3} more")

            file_io.log_run_state(state.week_number, {
                "node": "merge_section_or_revise",
                "action": "revision_requested",
                "section": current_section.id,
                "revision_count": state.revision_count,
                "education_approved": education_approved,
                "alpha_approved": alpha_approved,
                "total_issues": len(issues)
            })

        if tracer:
            tracer.trace_node_complete("merge_section_or_revise")
        return state

    def finalize_complete_week(self, state: RunState) -> RunState:
        """Compile final weekly content after all sections are approved"""
        tracer = get_tracer()
        if tracer:
            tracer.trace_node_start("finalize_complete_week")

        print(f"📚 Compiling final Week {state.week_number} content...")

        # Ensure all sections are completed
        if len(state.approved_sections) != len(state.sections):
            print(f"❌ Error: Expected {len(state.sections)} sections, got {len(state.approved_sections)}")
            file_io.log_run_state(state.week_number, {
                "node": "finalize_complete_week",
                "action": "error",
                "error": f"Incomplete sections: {len(state.approved_sections)}/{len(state.sections)}"
            })
            return state

        # Generate week title
        week_title = f"Data Science Week {state.week_number}"

        # First compile for document-level review
        final_path = file_io.compile_weekly_content(
            state.week_number,
            state.approved_sections,
            week_title,
            state.sections  # Pass section specs for proper titles
        )

        # Read the compiled document for review
        final_document_content = self.safe_file_operation(
            lambda: file_io.read_markdown_file(final_path),
            "read_final_document_for_review"
        )

        # Perform 1 document-level review iteration for performance
        for iteration in range(1):
            print(f"📋 Document-level review iteration {iteration + 1}/1")

            # EducationExpert document review
            document_review_approved = self._review_full_document(state, final_document_content, iteration + 1)

            if not document_review_approved:
                print(f"🔄 Document-level revision needed - recompiling")
                # If document needs revision, the sections have been updated
                # Recompile the final document
                final_path = file_io.compile_weekly_content(
                    state.week_number,
                    state.approved_sections,
                    week_title,
                    state.sections
                )
                # Read the updated document
                final_document_content = self.safe_file_operation(
                    lambda: file_io.read_markdown_file(final_path),
                    "read_updated_document_for_review"
                )
            else:
                print(f"✅ Document-level review {iteration + 1} passed")

        print(f"📚 Final document ready after 1 review iteration")

        # Calculate final statistics
        total_word_count = sum(s.word_count for s in state.approved_sections)
        total_citations = len(set().union(*[s.citations for s in state.approved_sections]))
        total_links = len(set().union(*[s.links for s in state.approved_sections]))

        print(f"✅ Week {state.week_number} compilation complete!")
        print(f"   📄 {len(state.approved_sections)} sections")
        print(f"   📝 ~{total_word_count} words total")
        print(f"   📚 {total_citations} unique citations")
        print(f"   🔗 {total_links} unique links")
        print(f"   💾 Saved to: {final_path}")

        file_io.log_run_state(state.week_number, {
            "node": "finalize_complete_week",
            "action": "week_completed",
            "final_path": final_path,
            "total_sections": len(state.approved_sections),
            "total_word_count": total_word_count,
            "total_citations": total_citations,
            "total_links": total_links,
            "autonomous_workflow": True
        })

        if tracer:
            tracer.trace_node_complete("finalize_complete_week")
        return state

    # =============================================================================
    # HELPER METHODS
    # =============================================================================

    def _extract_wlos_from_syllabus(self, syllabus_content: str, week_number: int) -> str:
        """Extract Weekly Learning Objectives from syllabus"""
        lines = syllabus_content.split('\n')
        wlo_section = []
        capturing = False

        for line in lines:
            if "learning objective" in line.lower() and (f"week {week_number}" in line.lower() or f"week{week_number}" in line.lower()):
                capturing = True
                wlo_section.append(line)
            elif capturing and line.strip() and line.startswith('#'):
                break
            elif capturing:
                wlo_section.append(line)

        return '\n'.join(wlo_section) if wlo_section else "Weekly Learning Objectives not found"

    def _extract_template_constraints(self, template_content: str, section_id: str) -> str:
        """Extract relevant template constraints for a section"""
        # Simple extraction - just return relevant portion
        return template_content[:800]  # First 800 chars as context

    def _extract_citations(self, content_md: str) -> List[str]:
        """Extract citations from markdown content"""
        citations = []

        # Look for markdown reference-style links [text](url)
        import re
        url_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
        matches = re.findall(url_pattern, content_md)

        for text, url in matches:
            citations.append(f"{text}: {url}")

        # Look for bibliography/reference sections
        lines = content_md.split('\n')
        in_references = False
        for line in lines:
            if 'references' in line.lower() or 'bibliography' in line.lower():
                in_references = True
            elif in_references and line.strip():
                citations.append(line.strip())

        return citations

    def _extract_wlo_mapping(self, content_md: str) -> Dict[str, str]:
        """Extract WLO mapping from content"""
        mapping = {}

        # Look for explicit WLO mentions
        import re
        wlo_pattern = r'WLO[:\s]*(\d+)'
        matches = re.findall(wlo_pattern, content_md)

        for i, match in enumerate(matches):
            mapping[f"wlo_{match}"] = f"Section addresses WLO {match}"

        return mapping

    def _review_full_document(self, state: RunState, document_content: str, iteration: int) -> bool:
        """Review the complete document for overall coherence and quality"""

        # Load guidelines for document review
        course_inputs = file_io.load_course_inputs(state.week_number)
        guidelines_content = self.safe_file_operation(
            lambda: file_io.read_markdown_file(course_inputs.guidelines_path),
            "read_guidelines_for_document_review"
        )

        # Document review prompt
        document_review_prompt = f"""
**COMPLETE WEEK {state.week_number} DOCUMENT TO REVIEW (ITERATION {iteration}/2):**
{document_content}

**COMPLETE AUTHORING GUIDELINES TO ENFORCE:**
{guidelines_content}

**DOCUMENT-LEVEL REVIEW REQUIREMENTS:**
You are reviewing the ENTIRE weekly document for overall quality, coherence, and guideline compliance.

CRITICAL DOCUMENT STANDARDS:
1. OVERALL COHERENCE: Does the entire document flow logically from section to section?
2. NARRATIVE CONSISTENCY: Is the narrative style consistent throughout all sections?
3. WLO COVERAGE: Are all Weekly Learning Objectives properly addressed across sections?
4. CITATION CONSISTENCY: Are citations properly integrated and consistently formatted?
5. ACADEMIC RIGOR: Does the entire document meet Master's level standards?
6. GUIDELINE COMPLIANCE: Does the entire document strictly follow ALL authoring guidelines?

REVIEW APPROACH:
- Read the entire document as a complete learning experience
- Check that sections build upon each other logically
- Ensure narrative prose throughout (no bullet points or lists anywhere)
- Verify all sections contribute to a coherent weekly learning experience
- Be extremely strict - this is iteration {iteration} of 2, demand excellence

Return a JSON object:
{{
  "approved": boolean,
  "required_fixes": ["document-level issue 1", "document-level issue 2"],
  "overall_quality_score": "1-10",
  "coherence_issues": ["flow issue 1", "flow issue 2"],
  "suggestions": ["improvement 1", "improvement 2"]
}}

Be very demanding - only approve if the document is truly excellent.
"""

        messages = [
            SystemMessage(content=PromptTemplates.get_education_expert_system()),
            HumanMessage(content=document_review_prompt)
        ]

        # Make the LLM call
        response = self.safe_llm_call(
            self.education_expert_llm,
            messages,
            context_info=f"document_review_iteration_{iteration}"
        )

        # Parse review response
        try:
            review_content = response.content if hasattr(response, 'content') else str(response)
            review_data = json.loads(review_content)

            approved = review_data.get("approved", False)
            required_fixes = review_data.get("required_fixes", [])

            if not approved:
                print(f"   📋 Document issues found:")
                for fix in required_fixes[:3]:
                    print(f"      • {fix}")

            return approved

        except (json.JSONDecodeError, Exception) as e:
            print(f"   ⚠️ Document review parsing failed, assuming approval")
            return True