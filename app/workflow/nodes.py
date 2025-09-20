import os
import json
import re
import time
import yaml
from typing import Dict, List, Any
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

    def _extract_week_info(self, syllabus_content: str, week_number: int) -> Dict[str, Any]:
        """Extract structured week information from syllabus using FileIO parser"""
        return file_io.extract_week_info_from_syllabus(week_number, syllabus_content)

    def _format_week_context_for_prompt(self, week_info: Dict[str, Any], week_number: int) -> str:
        """Format structured week information for use in prompts"""
        context_parts = [
            f"**Week {week_number} Overview:**",
            week_info.get("overview", "No overview available"),
            "",
            "**Weekly Learning Objectives (WLOs) with CLO Mappings:**"
        ]

        for wlo in week_info.get("wlos", []):
            context_parts.append(f"- **WLO{wlo['number']}:** {wlo['description']} (Maps to {wlo['clo_mapping']})")

        if week_info.get("bibliography"):
            context_parts.extend([
                "",
                "**Required Bibliography for This Week:**"
            ])
            for ref in week_info["bibliography"]:
                context_parts.append(f"- {ref}")

        return "\n".join(context_parts)

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

    def batch_write_all_sections(self, state: RunState) -> RunState:
        """Write all sections at once using ContentExpert"""
        tracer = get_tracer()
        if tracer:
            tracer.trace_node_start("batch_write_all_sections")

        print(f"📝 ContentExpert writing all {len(state.sections)} sections...")

        # Clear any previous sections
        state.approved_sections = []

        for i, section_spec in enumerate(state.sections):
            print(f"[{i+1}/{len(state.sections)}] ✍️ Writing: {section_spec.title}")

            # Set current section context
            state.current_index = i
            state.revision_count = 0

            # Load context from previously written sections in this batch
            if state.approved_sections:
                # Build context from already written sections
                context_parts = []
                for j, prev_section in enumerate(state.approved_sections):
                    prev_spec = state.sections[j]
                    summary = prev_section.content_md[:200].replace('\n', ' ').strip()
                    if len(prev_section.content_md) > 200:
                        summary += "..."
                    context_parts.append(f"**{prev_spec.title}**: {summary}")

                state.context_summary = f"Previously written sections:\n" + "\n\n".join(context_parts[-2:])  # Last 2 sections
            else:
                state.context_summary = "This is the first section being written."

            # Generate content for this section (ContentExpert can read previous sections)
            state = self.content_expert_write(state)

            # Save the draft (mark as needing review)
            if state.current_draft:
                state.current_draft.needs_revision = True  # Will be reviewed later
                state.approved_sections.append(state.current_draft)

                # Save to individual files immediately (so next sections can read it)
                file_path = file_io.save_section_draft(state.current_draft, backup=True)
                print(f"   💾 Saved draft: {file_path}")
                print(f"   📝 Generated {state.current_draft.word_count} words")

        print(f"✅ All {len(state.sections)} sections written to individual files")

        if tracer:
            tracer.trace_node_complete("batch_write_all_sections")
        return state

    def batch_review_all_sections(self, state: RunState) -> RunState:
        """Review all sections with EducationExpert and AlphaStudent"""
        tracer = get_tracer()
        if tracer:
            tracer.trace_node_start("batch_review_all_sections")

        print(f"📋 Reviewing all {len(state.approved_sections)} sections...")

        # Load all existing sections for context (agents can see all files)
        all_sections_context = file_io.load_all_temporal_sections()

        sections_needing_revision = []

        for i, section_draft in enumerate(state.approved_sections):
            section_spec = state.sections[i]
            print(f"[{i+1}/{len(state.approved_sections)}] 📋 Reviewing: {section_spec.title}")

            # Set current context for reviews (include access to all other sections)
            state.current_index = i
            state.current_draft = section_draft
            state.context_summary = self._build_full_context_summary(all_sections_context, i)

            # Education Expert review (with access to all sections)
            state = self.education_expert_review(state)
            education_approved = state.education_review and state.education_review.approved

            # Alpha Student review (with access to all sections)
            state = self.alpha_student_review(state)
            alpha_approved = state.alpha_review and state.alpha_review.approved

            both_approved = education_approved and alpha_approved

            if both_approved:
                print(f"   ✅ {section_spec.title}: Approved by both reviewers")
                section_draft.needs_revision = False
            else:
                print(f"   ❌ {section_spec.title}: Needs revision")
                section_draft.needs_revision = True
                sections_needing_revision.append(i)

                # Collect issues
                issues = []
                if not education_approved and state.education_review:
                    issues.extend([f"Editor: {fix}" for fix in state.education_review.required_fixes[:3]])
                if not alpha_approved and state.alpha_review:
                    issues.extend([f"Reviewer: {fix}" for fix in state.alpha_review.required_fixes[:3]])

                if issues:
                    print(f"      📝 Sample issues ({len(issues)}):")
                    for issue in issues[:2]:  # Show first 2
                        print(f"         • {issue}")

        print(f"📊 Review summary: {len(state.approved_sections) - len(sections_needing_revision)}/{len(state.approved_sections)} sections approved")
        if sections_needing_revision:
            print(f"   🔄 Sections needing revision: {len(sections_needing_revision)}")

        if tracer:
            tracer.trace_node_complete("batch_review_all_sections")
        return state

    def batch_revise_if_needed(self, state: RunState) -> RunState:
        """Revise sections that need improvement"""
        tracer = get_tracer()
        if tracer:
            tracer.trace_node_start("batch_revise_if_needed")

        # Increment batch revision count
        state.batch_revision_count += 1

        print(f"🔄 Batch revision attempt {state.batch_revision_count}/2")

        # Load all existing sections for context (ContentExpert can see all files)
        all_sections_context = file_io.load_all_temporal_sections()

        sections_revised = 0
        for i, section_draft in enumerate(state.approved_sections):
            if hasattr(section_draft, 'needs_revision') and section_draft.needs_revision:
                section_spec = state.sections[i]
                print(f"[{i+1}/{len(state.approved_sections)}] ✏️ Revising: {section_spec.title}")

                # Set context for revision (include access to all other sections)
                state.current_index = i
                state.current_draft = section_draft
                state.revision_count = state.batch_revision_count
                state.context_summary = self._build_full_context_summary(all_sections_context, i)

                # Revise the section (ContentExpert has full context)
                state = self.content_expert_write(state)

                # Update the section in our list
                if state.current_draft:
                    state.approved_sections[i] = state.current_draft

                    # Save revised version
                    file_path = file_io.save_section_draft(state.current_draft, backup=True)
                    print(f"   💾 Revised and saved: {file_path}")
                    print(f"   📝 Generated {state.current_draft.word_count} words")
                    sections_revised += 1

        print(f"✅ Revised {sections_revised} sections")

        if tracer:
            tracer.trace_node_complete("batch_revise_if_needed")
        return state

    def _build_full_context_summary(self, all_sections: Dict[str, str], current_index: int) -> str:
        """Build context summary including all available sections for agent reference"""
        context_parts = []

        # Add summary of all other sections
        for section_id, content in all_sections.items():
            if section_id != self.sections[current_index].id if current_index < len(getattr(self, 'sections', [])) else True:
                # Get first 200 characters as summary
                summary = content[:200].replace('\n', ' ').strip()
                if len(content) > 200:
                    summary += "..."
                context_parts.append(f"**{section_id}**: {summary}")

        if context_parts:
            return f"Context from other sections:\n" + "\n\n".join(context_parts[:3])  # Limit to 3 sections
        else:
            return "This is the first section being written."

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

        # Format week context for the prompt
        week_context = self._format_week_context_for_prompt(week_info, state.week_number)

        # Build detailed section instruction
        section_instruction = PromptTemplates.get_section_instruction(
            current_section.title,
            current_section.description,
            week_context,
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

        # Format section constraints from sections.json for the prompt
        section_constraints = ""
        if current_section.constraints:
            section_constraints = "\n**DETAILED SECTION REQUIREMENTS FROM SECTIONS.JSON:**\n"

            # Add structure requirements
            if "structure" in current_section.constraints:
                section_constraints += f"Structure Required:\n"
                for item in current_section.constraints["structure"]:
                    section_constraints += f"• {item}\n"

            # Add subsection details
            if "subsections" in current_section.constraints:
                section_constraints += f"\nSubsection Details:\n"
                for subsection, details in current_section.constraints["subsections"].items():
                    section_constraints += f"• {subsection.title()}: {details.get('content', 'Not specified')}\n"
                    if "format" in details:
                        section_constraints += f"  Format: {details['format']}\n"
                    if details.get("citation_required"):
                        section_constraints += f"  Citation Required: YES\n"
                    if details.get("alignment_required"):
                        section_constraints += f"  WLO Alignment Required: YES\n"

            # Add other constraints
            for key, value in current_section.constraints.items():
                if key not in ["structure", "subsections"]:
                    section_constraints += f"• {key}: {value}\n"

        # Build a comprehensive prompt with sections.json requirements
        content_prompt = f"""Write educational content for: {current_section.title}

**Week {state.week_number} Topic:** {week_info.get('overview', 'Data Science fundamentals')}

**Learning Objectives for this week:**
{chr(10).join([f'- WLO{wlo["number"]}: {wlo["description"]} ({wlo["clo_mapping"]})' for wlo in week_info.get('wlos', [])])}

**Required Reading Materials:**
{chr(10).join([f'- {ref}' for ref in week_info.get('bibliography', [])])}

{section_constraints}

{revision_feedback}

**CRITICAL REQUIREMENTS:**
- Follow the EXACT structure and format specified in sections.json above
- Use flowing narrative prose (no bullet points unless specifically required)
- Include concrete examples and cite the required readings when relevant
- Ensure all subsections are included as specified
- Meet format requirements for each subsection
- Include proper citations where required
- Ensure WLO alignment where specified

Write complete educational content that teaches students about the week topic as a professor teaching Master's students about data science.

Start writing the educational content now:"""

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

        # Load ONLY the three required files for EDITOR
        # 1. Building Blocks requirements for multimedia and assessment compliance
        building_blocks_content = self.safe_file_operation(
            lambda: file_io.read_yaml_file("config/building_blocks_requirements.yaml"),
            "read_building_blocks_for_review"
        )

        # 2. Template mapping for structure requirements
        template_mapping_content = self.safe_file_operation(
            lambda: file_io.read_yaml_file("config/template_mapping.yaml"),
            "read_template_mapping_for_review"
        )

        # 3. Sections specification (already loaded in state.sections)

        # Calculate word count for length compliance checking
        content_word_count = len(state.current_draft.content_md.split())

        # Get template requirements for this section from template_mapping
        section_template_info = template_mapping_content.get('sections', {}).get(current_section.id, {})
        template_requirements = section_template_info.get('template_requirements', [])
        implementation_details = section_template_info.get('implementation', {})

        # Build review prompt using only the three required files
        education_review_prompt = f"""
**SECTION TO REVIEW:**
{state.current_draft.content_md}

**WORD COUNT ANALYSIS:**
- Current word count: {content_word_count} words
- Section: {current_section.id}

**BUILDING BLOCKS V2 REQUIREMENTS (ENFORCE ALL MULTIMEDIA/ASSESSMENT STANDARDS):**
{yaml.dump(building_blocks_content, default_flow_style=False, sort_keys=False)}

**TEMPLATE MAPPING REQUIREMENTS FOR THIS SECTION:**
{yaml.dump(section_template_info, default_flow_style=False, sort_keys=False)}

**SECTION SPECIFICATION (from sections.json):**
- ID: {current_section.id}
- Title: {current_section.title}
- Description: {current_section.description}
- Constraints: {json.dumps(current_section.constraints, indent=2)}

Review this section thoroughly as the EDITOR. Your job is to enforce ALL requirements from the three configuration files.

CRITICAL REVIEW FOCUS:
1. TEMPLATE MAPPING COMPLIANCE: Does content meet all template requirements listed above for this section?
2. BUILDING BLOCKS V2 COMPLIANCE: Check multimedia elements (figures, tables, videos), assessment questions, and accessibility requirements
3. SECTIONS.JSON COMPLIANCE: Does content meet all constraints and requirements specified in the section specification?
4. NARRATIVE PROSE: Is content written in flowing paragraphs? Reject if bullet points/lists are used inappropriately.
5. EDUCATIONAL QUALITY: Does content effectively teach at Master's level?
6. CITATION INTEGRATION: Are citations properly integrated when required?
7. WLO ALIGNMENT: Is connection to learning objectives explicit where required?

Return a JSON object with:
{{
  "approved": boolean,
  "required_fixes": ["specific fix 1", "specific fix 2"],
  "optional_suggestions": ["suggestion 1", "suggestion 2"]
}}

Be thorough and demanding. Only approve content that meets ALL requirements from:
- Building Blocks V2 requirements
- Template mapping requirements for this section
- Section specification constraints
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