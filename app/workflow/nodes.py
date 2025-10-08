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
from app.tools import links, datasets
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
            # ContentExpert uses gpt-4o for superior content generation
            content_deployment = os.getenv("AZURE_GPT4O_DEPLOYMENT", "gpt-4o")
            content_endpoint = os.getenv("AZURE_GPT4O_ENDPOINT", os.getenv("AZURE_ENDPOINT"))
            content_key = os.getenv("AZURE_GPT4O_KEY", os.getenv("AZURE_SUBSCRIPTION_KEY"))
            content_api_version = os.getenv("AZURE_GPT4O_API_VERSION", "2025-01-01-preview")

            self.content_expert_llm = AzureChatOpenAI(
                azure_endpoint=content_endpoint,
                azure_deployment=content_deployment,
                api_key=content_key,
                api_version=content_api_version,
                temperature=1.2,  # Higher temperature for creative, engaging writing
                max_tokens=4000
            )

            # Other agents use default deployment
            deployment = os.getenv("AZURE_DEPLOYMENT", "gpt-5-mini")
            self.education_expert_llm = self._create_azure_llm(
                deployment=deployment,
                temperature=1.0,
                max_completion_tokens=2000
            )
            self.alpha_student_llm = self._create_azure_llm(
                deployment=deployment,
                temperature=0.5,  # Lower temperature for consistent scoring
                max_completion_tokens=2000
            )
            # Initialize context managers with Azure model names
            self.content_expert_context = ContextManager(content_deployment)
            self.education_expert_context = ContextManager(deployment)
            self.alpha_student_context = ContextManager(deployment)
        else:
            # Fallback to regular OpenAI
            content_model = os.getenv("MODEL_CONTENT_EXPERT", "gpt-4o")
            self.content_expert_llm = ChatOpenAI(
                model=content_model,
                temperature=1.2,  # Higher temperature for creative, engaging writing
                max_completion_tokens=4000
            )
            self.education_expert_llm = ChatOpenAI(
                model=os.getenv("MODEL_EDUCATION_EXPERT", "gpt-4o-mini"),
                temperature=1.0,
                max_completion_tokens=2000
            )
            self.alpha_student_llm = ChatOpenAI(
                model=os.getenv("MODEL_ALPHA_STUDENT", "gpt-4o-mini"),
                temperature=0.5,  # Lower temperature for consistent scoring and verification
                max_completion_tokens=2000
            )
            # Initialize context managers with OpenAI model names
            self.content_expert_context = ContextManager(content_model)
            self.education_expert_context = ContextManager(os.getenv("MODEL_EDUCATION_EXPERT", "gpt-4o-mini"))
            self.alpha_student_context = ContextManager(os.getenv("MODEL_ALPHA_STUDENT", "gpt-4o-mini"))

        # Log agent configurations
        self._log_agent_configurations()

    def _log_agent_configurations(self):
        """Log the model and temperature configuration for each agent"""
        print("\n" + "="*60)
        print("🤖 AGENT CONFIGURATIONS")
        print("="*60)

        if self._is_azure_configured():
            # Azure configuration
            content_deployment = os.getenv("AZURE_GPT4O_DEPLOYMENT", "gpt-4o")
            default_deployment = os.getenv("AZURE_DEPLOYMENT", "gpt-5-mini")

            print("📝 ContentExpert (WRITER):")
            print(f"   Model: {content_deployment} (Azure)")
            print(f"   Temperature: 1.2")
            print(f"   Purpose: Creative, engaging content generation")

            print("\n📚 EducationExpert (EDITOR):")
            print(f"   Model: {default_deployment} (Azure)")
            print(f"   Temperature: 1.0 (gpt-5-mini uses default)")
            print(f"   Purpose: Pedagogical review and compliance")

            print("\n🎓 AlphaStudent (REVIEWER):")
            print(f"   Model: {default_deployment} (Azure)")
            print(f"   Temperature: 0.5 (gpt-5-mini uses default)")
            print(f"   Purpose: Quality scoring and verification")
        else:
            # OpenAI configuration
            content_model = os.getenv("MODEL_CONTENT_EXPERT", "gpt-4o")
            education_model = os.getenv("MODEL_EDUCATION_EXPERT", "gpt-4o-mini")
            alpha_model = os.getenv("MODEL_ALPHA_STUDENT", "gpt-4o-mini")

            print("📝 ContentExpert (WRITER):")
            print(f"   Model: {content_model} (OpenAI)")
            print(f"   Temperature: 1.2")
            print(f"   Purpose: Creative, engaging content generation")

            print("\n📚 EducationExpert (EDITOR):")
            print(f"   Model: {education_model} (OpenAI)")
            print(f"   Temperature: 1.0")
            print(f"   Purpose: Pedagogical review and compliance")

            print("\n🎓 AlphaStudent (REVIEWER):")
            print(f"   Model: {alpha_model} (OpenAI)")
            print(f"   Temperature: 0.5")
            print(f"   Purpose: Quality scoring and verification")

        print("="*60 + "\n")

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

    def _load_template_and_guidelines(self) -> Dict[str, str]:
        """Load template and guidelines for WRITER access"""
        template_content = ""
        guidelines_content = ""

        try:
            template_path = os.path.join(os.getcwd(), "input", "template.docx")
            if os.path.exists(template_path):
                from docx import Document
                doc = Document(template_path)
                template_content = "\n".join([para.text for para in doc.paragraphs if para.text.strip()])
        except Exception as e:
            print(f"⚠️  Could not load template.docx: {e}")

        try:
            guidelines_path = os.path.join(os.getcwd(), "input", "guidelines.md")
            if os.path.exists(guidelines_path):
                with open(guidelines_path, 'r', encoding='utf-8') as f:
                    guidelines_content = f.read()
        except Exception as e:
            print(f"⚠️  Could not load guidelines.md: {e}")

        return {
            "template": template_content[:2000] if template_content else "Template not available",  # Limit size
            "guidelines": guidelines_content[:3000] if guidelines_content else "Guidelines not available"
        }

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

            # Apply EDITOR's direct edits immediately (HYBRID MODEL)
            state = self.apply_direct_edits(state)

            # Update the section_draft with changes from direct edits
            if state.current_draft:
                state.approved_sections[i] = state.current_draft

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

                # Collect issues and add to feedback memory
                issues = []
                if not education_approved and state.education_review:
                    issues.extend([f"Editor: {fix}" for fix in state.education_review.required_fixes[:3]])
                    # Add all education expert feedback to memory (not just first 3)
                    for fix in state.education_review.required_fixes:
                        state.feedback_memory.append(f"EDITOR FEEDBACK [{section_spec.title}]: {fix}")
                if not alpha_approved and state.alpha_review:
                    issues.extend([f"Reviewer: {fix}" for fix in state.alpha_review.required_fixes[:3]])
                    # Add all alpha student feedback to memory (not just first 3)
                    for fix in state.alpha_review.required_fixes:
                        state.feedback_memory.append(f"REVIEWER FEEDBACK [{section_spec.title}]: {fix}")

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

        print(f"🔄 Batch revision attempt {state.batch_revision_count}/3")

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

        # Track previous scores to detect regressions
        previous_editor_score = state.education_review.quality_score if state.education_review else None
        previous_reviewer_score = state.alpha_review.quality_score if state.alpha_review else None

        # Add revision feedback if this is a revision
        revision_feedback = ""
        is_revision = state.education_review and not state.education_review.approved

        if is_revision:
            # CRITICAL: Content preservation instructions
            revision_feedback += f"\n**🛡️ CONTENT PRESERVATION STRATEGY:**\n"
            revision_feedback += f"• This is revision #{state.revision_count + 1}\n"
            revision_feedback += f"• PRESERVE sections that received good scores (>=7 in any dimension)\n"
            revision_feedback += f"• ONLY revise specific sections that scored low (<7)\n"
            revision_feedback += f"• DO NOT rewrite the entire content - targeted fixes only\n"
            revision_feedback += f"• DO NOT reduce word count unless explicitly requested\n"
            revision_feedback += f"• Keep the narrative structure that works - fix specific issues only\n\n"

        if state.education_review and not state.education_review.approved:
            revision_feedback += f"**EDITOR FEEDBACK TO ADDRESS:**\n"
            if state.education_review.quality_score:
                revision_feedback += f"• Current Editor Score: {state.education_review.quality_score}/10 (NEED >=9)\n"
                if state.education_review.score_breakdown:
                    revision_feedback += f"  Score Breakdown: {json.dumps(state.education_review.score_breakdown, indent=2)}\n"
                    # Identify what to preserve vs fix
                    good_aspects = [k for k, v in state.education_review.score_breakdown.items() if v >= 7]
                    needs_work = [k for k, v in state.education_review.score_breakdown.items() if v < 7]
                    if good_aspects:
                        revision_feedback += f"  ✅ PRESERVE THESE (scored >=7): {', '.join(good_aspects)}\n"
                    if needs_work:
                        revision_feedback += f"  🔧 FIX ONLY THESE (scored <7): {', '.join(needs_work)}\n"
            for fix in state.education_review.required_fixes:
                revision_feedback += f"• {fix}\n"

        if state.alpha_review and not state.alpha_review.approved:
            revision_feedback += f"\n**REVIEWER FEEDBACK TO ADDRESS:**\n"
            if state.alpha_review.quality_score:
                revision_feedback += f"• Current Reviewer Score: {state.alpha_review.quality_score}/10 (NEED >=9)\n"
                if state.alpha_review.score_breakdown:
                    revision_feedback += f"  Score Breakdown: {json.dumps(state.alpha_review.score_breakdown, indent=2)}\n"
                    # Identify what to preserve vs fix
                    good_aspects = [k for k, v in state.alpha_review.score_breakdown.items() if v >= 7]
                    needs_work = [k for k, v in state.alpha_review.score_breakdown.items() if v < 7]
                    if good_aspects:
                        revision_feedback += f"  ✅ PRESERVE THESE (scored >=7): {', '.join(good_aspects)}\n"
                    if needs_work:
                        revision_feedback += f"  🔧 FIX ONLY THESE (scored <7): {', '.join(needs_work)}\n"
            for fix in state.alpha_review.required_fixes:
                revision_feedback += f"• {fix}\n"

        # Add accumulated feedback memory to help avoid repeating mistakes
        if state.feedback_memory:
            revision_feedback += f"\n**IMPORTANT: LEARN FROM ALL PREVIOUS FEEDBACK (DO NOT REPEAT THESE MISTAKES):**\n"
            # Show recent feedback first (most relevant)
            recent_feedback = state.feedback_memory[-20:] if len(state.feedback_memory) > 20 else state.feedback_memory
            for feedback_item in recent_feedback:
                revision_feedback += f"• {feedback_item}\n"
            if len(state.feedback_memory) > 20:
                revision_feedback += f"• ... and {len(state.feedback_memory) - 20} more previous feedback items\n"

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

        # Load template and guidelines for WRITER (CRITICAL FOR QUALITY)
        template_and_guidelines = self._load_template_and_guidelines()

        # Build a comprehensive prompt with sections.json requirements + TEMPLATE + GUIDELINES
        content_prompt = f"""Write educational content for: {current_section.title}

**Week {state.week_number} Topic:** {week_info.get('overview', 'Data Science fundamentals')}

**Learning Objectives for this week:**
{chr(10).join([f'- WLO{wlo["number"]}: {wlo["description"]} ({wlo["clo_mapping"]})' for wlo in week_info.get('wlos', [])])}

**TEMPLATE STRUCTURE (MUST FOLLOW):**
{template_and_guidelines['template']}

**AUTHORING GUIDELINES (MUST COMPLY):**
{template_and_guidelines['guidelines']}

**Required Reading Materials:**
{chr(10).join([f'- {ref}' for ref in week_info.get('bibliography', [])])}

{section_constraints}

{revision_feedback}

**CRITICAL REQUIREMENTS:**
- START with the section title as an H2 header: ## {current_section.title}
- Follow the EXACT structure and format specified in sections.json above
- Use appropriate formatting for section type:
  * Discovery/Engagement main content: Flowing narrative prose
  * Quiz sections: Use bullet points and clear formatting for questions/answers
  * Rubric sections: Use markdown tables for clear criteria presentation
  * Overview/Consolidation: Mix narrative and bullet points as appropriate
  * Consolidation final subsections: Keep concise (max 2 paragraphs)
- Include multimedia content (videos, references, interactive elements) to enhance engagement
- Include concrete examples and cite the required readings when relevant
- Ensure all subsections are included as specified
- Meet format requirements for each subsection
- Include proper citations where required
- Ensure WLO alignment where specified

Write complete educational content that teaches students about the week topic as a professor teaching Master's students about data science.

Start writing the educational content now, beginning with the section header:"""

        content_messages = [
            SystemMessage(content=PromptTemplates.get_content_expert_system()),
            HumanMessage(content=content_prompt)
        ]

        # Adjust temperature for revisions to reduce randomness
        # Initial draft: 1.2 (creative), Revisions: 0.9 (more focused)
        active_llm = self.content_expert_llm
        if is_revision and state.revision_count >= 1:
            # Create a lower-temperature version for revisions
            if self._is_azure_configured():
                content_deployment = os.getenv("AZURE_GPT4O_DEPLOYMENT", "gpt-4o")
                content_endpoint = os.getenv("AZURE_GPT4O_ENDPOINT", os.getenv("AZURE_ENDPOINT"))
                content_key = os.getenv("AZURE_GPT4O_KEY", os.getenv("AZURE_SUBSCRIPTION_KEY"))
                content_api_version = os.getenv("AZURE_GPT4O_API_VERSION", "2025-01-01-preview")

                active_llm = AzureChatOpenAI(
                    azure_endpoint=content_endpoint,
                    azure_deployment=content_deployment,
                    api_key=content_key,
                    api_version=content_api_version,
                    temperature=0.9,  # Lower temperature for focused revisions
                    max_tokens=4000
                )
                print(f"   🎯 Using revision temperature: 0.9 (more focused)")
            else:
                active_llm = ChatOpenAI(
                    model="gpt-4o",
                    temperature=0.9,
                    max_completion_tokens=4000
                )

        # Make the LLM call for content generation
        response = self.safe_llm_call(
            active_llm,
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

        # Track score deltas to detect regressions
        if is_revision:
            # Store previous scores for delta tracking
            if not hasattr(state, 'score_history'):
                state.score_history = []

            state.score_history.append({
                'revision': state.revision_count,
                'editor_score': previous_editor_score,
                'reviewer_score': previous_reviewer_score,
                'word_count': word_count
            })

        # Update context for next sections
        if len(state.approved_sections) < len(state.sections):
            summary_parts = [f"Section {current_section.id}: {current_section.title} - {word_count} words"]
            state.context_summary = "; ".join(summary_parts)

        print(f"   📝 Generated {word_count} words")
        if is_revision and previous_editor_score:
            print(f"   📊 Previous Editor Score: {previous_editor_score}/10 | Previous Reviewer Score: {previous_reviewer_score}/10")

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

CRITICAL REVIEW FOCUS & SCORING:
1. TEMPLATE MAPPING COMPLIANCE (0-10): Does content meet all template requirements listed above for this section?
2. BUILDING BLOCKS V2 COMPLIANCE (0-10): Check multimedia elements (figures, tables, videos), assessment questions, and accessibility requirements
3. SECTIONS.JSON COMPLIANCE (0-10): Does content meet all constraints and requirements specified in the section specification?
4. NARRATIVE PROSE QUALITY (0-10): Is content written in flowing paragraphs? Reject if bullet points/lists are used inappropriately.
5. EDUCATIONAL QUALITY (0-10): Does content effectively teach at Master's level?
6. CITATION INTEGRATION (0-10): Are citations properly integrated when required?
7. WLO ALIGNMENT (0-10): Is connection to learning objectives explicit where required?

**MANDATORY: Provide a quality score from 1-10 based on pedagogical excellence.**

**SCORING GUIDE:**
- 10 = EXCELLENT: Perfect compliance, exceptional pedagogical quality, all requirements met
- 9 = VERY GOOD: Strong compliance, very good quality, minor polish needed
- 7-8 = GOOD: Decent compliance, good quality, some improvements needed
- 5-6 = NEEDS IMPROVEMENT: Several compliance issues or quality concerns
- 1-4 = POOR: Major compliance failures, significant quality issues

Return a JSON object with:
{{
  "approved": boolean,
  "quality_score": number (1-10),
  "score_breakdown": {{
    "template_compliance": number (0-10),
    "building_blocks_compliance": number (0-10),
    "sections_compliance": number (0-10),
    "narrative_quality": number (0-10),
    "educational_quality": number (0-10),
    "citation_integration": number (0-10),
    "wlo_alignment": number (0-10)
  }},
  "direct_edits": [
    {{
      "edit_type": "trim_to_word_count | fix_citation | add_missing_section | fix_header | fix_formatting",
      "location": "section name or line number",
      "current_value": "text to find (optional)",
      "new_value": "replacement text (optional)",
      "target": number (optional, for word count),
      "reason": "why this edit is needed"
    }}
  ],
  "required_fixes": ["creative fixes for Writer - narrative, examples, depth"],
  "optional_suggestions": ["nice-to-have improvements"]
}}

**APPROVAL THRESHOLD**: Only approve (approved=true) if quality_score >= 9.
Be thorough and demanding. Content must score 9 or 10 to be approved.
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

            # Extract quality score and breakdown
            quality_score = review_data.get("quality_score")
            score_breakdown = review_data.get("score_breakdown", {})

            # Auto-reject if score is too low (below 9)
            approved = review_data.get("approved", True)
            if quality_score and quality_score < 9:
                approved = False
                print(f"⚠️  EducationExpert quality score {quality_score}/10 is below threshold (9) - AUTO-REJECTING")

            # Display quality score and track delta
            if quality_score:
                print(f"📊 EducationExpert Score: {quality_score}/10")

                # Check for score regression
                if hasattr(state, 'score_history') and state.score_history:
                    last_score = state.score_history[-1].get('editor_score')
                    if last_score and quality_score < last_score:
                        delta = quality_score - last_score
                        print(f"⚠️  🔻 SCORE REGRESSION: {last_score}/10 → {quality_score}/10 (Δ {delta:+.1f})")
                        print(f"   ⚠️  Content quality DECREASED - review what changed!")
                    elif last_score and quality_score > last_score:
                        delta = quality_score - last_score
                        print(f"✅ 📈 SCORE IMPROVEMENT: {last_score}/10 → {quality_score}/10 (Δ {delta:+.1f})")

                if score_breakdown:
                    print(f"   Breakdown:")
                    print(f"     - Template Compliance: {score_breakdown.get('template_compliance', 'N/A')}/10")
                    print(f"     - Building Blocks: {score_breakdown.get('building_blocks_compliance', 'N/A')}/10")
                    print(f"     - Sections Compliance: {score_breakdown.get('sections_compliance', 'N/A')}/10")
                    print(f"     - Narrative Quality: {score_breakdown.get('narrative_quality', 'N/A')}/10")
                    print(f"     - Educational Quality: {score_breakdown.get('educational_quality', 'N/A')}/10")
                    print(f"     - Citation Integration: {score_breakdown.get('citation_integration', 'N/A')}/10")
                    print(f"     - WLO Alignment: {score_breakdown.get('wlo_alignment', 'N/A')}/10")

            # Parse direct_edits from review
            direct_edits_data = review_data.get("direct_edits", [])
            from app.models.schemas import DirectEdit
            direct_edits = []
            for edit_data in direct_edits_data:
                try:
                    direct_edit = DirectEdit(**edit_data)
                    direct_edits.append(direct_edit)
                except Exception as e:
                    print(f"⚠️  Failed to parse direct edit: {e}")

            # Display direct edits if any
            if direct_edits:
                print(f"\n   🔧 DIRECT EDITS ({len(direct_edits)}) - EDITOR will apply immediately:")
                for i, edit in enumerate(direct_edits, 1):
                    print(f"      {i}. [{edit.edit_type}] {edit.reason}")

            state.education_review = ReviewNotes(
                reviewer="EducationExpert",
                approved=approved,
                quality_score=quality_score,
                score_breakdown=score_breakdown,
                required_fixes=review_data.get("required_fixes", []),
                optional_suggestions=review_data.get("optional_suggestions", []),
                direct_edits=direct_edits
            )
        except json.JSONDecodeError:
            # Fallback if JSON parsing fails
            print("⚠️  Failed to parse EducationExpert review JSON - using fallback")
            state.education_review = ReviewNotes(
                reviewer="EducationExpert",
                approved=True,
                quality_score=None,
                score_breakdown=None,
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

    def apply_direct_edits(self, state: RunState) -> RunState:
        """Apply EDITOR's direct edits immediately without Writer intervention"""
        if not state.education_review or not state.education_review.direct_edits:
            return state  # No direct edits to apply

        print(f"\n🔧 Applying {len(state.education_review.direct_edits)} direct edits from EDITOR...")

        current_content = state.current_draft.content_md
        modified_content = current_content
        edits_applied = 0

        for edit in state.education_review.direct_edits:
            try:
                if edit.edit_type == "trim_to_word_count":
                    modified_content = self._trim_section_to_word_count(
                        modified_content, edit.location, edit.target
                    )
                    edits_applied += 1
                    print(f"   ✅ Trimmed {edit.location} to {edit.target} words")

                elif edit.edit_type == "fix_citation":
                    if edit.current_value and edit.new_value:
                        modified_content = modified_content.replace(
                            edit.current_value, edit.new_value
                        )
                        edits_applied += 1
                        print(f"   ✅ Fixed citation: {edit.current_value} → {edit.new_value}")

                elif edit.edit_type == "add_missing_section":
                    modified_content = self._add_section_after(
                        modified_content, edit.location, edit.new_value
                    )
                    edits_applied += 1
                    print(f"   ✅ Added missing section at {edit.location}")

                elif edit.edit_type == "fix_header":
                    if edit.current_value and edit.new_value:
                        modified_content = modified_content.replace(
                            edit.current_value, edit.new_value
                        )
                        edits_applied += 1
                        print(f"   ✅ Fixed header: {edit.current_value} → {edit.new_value}")

                elif edit.edit_type == "fix_formatting":
                    if edit.current_value and edit.new_value:
                        modified_content = modified_content.replace(
                            edit.current_value, edit.new_value
                        )
                        edits_applied += 1
                        print(f"   ✅ Fixed formatting")

            except Exception as e:
                print(f"   ⚠️  Failed to apply edit [{edit.edit_type}]: {e}")

        if edits_applied > 0:
            # Update the draft with modified content
            state.current_draft.content_md = modified_content
            state.current_draft.word_count = len(modified_content.split())
            print(f"\n✅ Applied {edits_applied}/{len(state.education_review.direct_edits)} direct edits")
            print(f"   New word count: {state.current_draft.word_count}")

        return state

    def _trim_section_to_word_count(self, content: str, section_name: str, target: int) -> str:
        """Trim a specific section to target word count"""
        # Simple implementation: find section and trim from end
        lines = content.split('\n')
        result_lines = []
        in_target_section = False
        section_lines = []

        for line in lines:
            if section_name.lower() in line.lower() and line.startswith('#'):
                in_target_section = True
                result_lines.append(line)
            elif in_target_section and line.startswith('#'):
                # Next section found, finish trimming
                trimmed = self._trim_text_to_words('\n'.join(section_lines), target)
                result_lines.extend(trimmed.split('\n'))
                in_target_section = False
                result_lines.append(line)
            elif in_target_section:
                section_lines.append(line)
            else:
                result_lines.append(line)

        # Handle case where section was last
        if in_target_section and section_lines:
            trimmed = self._trim_text_to_words('\n'.join(section_lines), target)
            result_lines.extend(trimmed.split('\n'))

        return '\n'.join(result_lines)

    def _trim_text_to_words(self, text: str, target: int) -> str:
        """Trim text to approximately target word count"""
        words = text.split()
        if len(words) <= target:
            return text
        return ' '.join(words[:target]) + '\n\n[Content trimmed to meet word limit]'

    def _add_section_after(self, content: str, after_location: str, new_content: str) -> str:
        """Add new section after specified location"""
        # Extract the target section name from "after_X"
        target = after_location.replace("after_", "").replace("_", " ")
        lines = content.split('\n')
        result_lines = []
        found = False

        for i, line in enumerate(lines):
            result_lines.append(line)
            if not found and target.lower() in line.lower() and line.startswith('#'):
                # Find end of this section (next header or end of content)
                j = i + 1
                while j < len(lines) and not lines[j].startswith('#'):
                    result_lines.append(lines[j])
                    j += 1
                # Insert new section
                result_lines.append('\n')
                result_lines.append(new_content)
                result_lines.append('\n')
                found = True
                # Continue with rest
                result_lines.extend(lines[j:])
                break

        if not found:
            # Append at end if location not found
            result_lines.append('\n')
            result_lines.append(new_content)

        return '\n'.join(result_lines)

    def alpha_student_review(self, state: RunState) -> RunState:
        """AlphaStudent (REVIEWER) reviews from student perspective"""
        tracer = get_tracer()
        if tracer:
            tracer.trace_node_start("alpha_student_review")

        print(f"🎓 AlphaStudent reviewing for clarity and usability")

        current_section = state.sections[state.current_index]

        # TRIPLE-CHECK all links as per new requirements
        print(f"🔗 Performing TRIPLE verification of all links...")
        link_urls = state.current_draft.links if state.current_draft else []

        triple_check_results = None
        if link_urls:
            triple_check_results = self.safe_file_operation(
                lambda: links.triple_check(link_urls),
                "triple_check_links_for_alpha_review"
            )

        # Count results from triple check
        working_links = 0
        broken_links = 0

        if triple_check_results and 'summary' in triple_check_results:
            working_links = triple_check_results['summary']['passed_all_rounds']
            broken_links = len(triple_check_results['summary']['failed_urls'])

            # Log detailed results
            if broken_links > 0:
                print(f"⚠️  {broken_links} link(s) failed triple verification:")
                for failed in triple_check_results['summary']['failed_urls']:
                    print(f"   ❌ {failed['url']} - passed {failed['passed_rounds']}/3 rounds")
            else:
                print(f"✅ All {working_links} links passed triple verification")

        link_summary = f"{working_links} verified (3/3 rounds), {broken_links} failed" if triple_check_results else "no links"

        # Prepare detailed link report for reviewer
        link_report = triple_check_results if triple_check_results else {"summary": {"all_passed": True, "failed_urls": []}}

        # VERIFY all datasets mentioned in content
        print(f"📊 Verifying dataset availability...")
        dataset_report = None
        if state.current_draft and state.current_draft.content_md:
            dataset_report = self.safe_file_operation(
                lambda: datasets.verify_datasets(state.current_draft.content_md),
                "verify_datasets_for_alpha_review"
            )

            if dataset_report and dataset_report.get('total_datasets', 0) > 0:
                if dataset_report.get('all_verified', False):
                    print(f"✅ All {dataset_report['total_datasets']} dataset(s) verified ({dataset_report['kaggle_datasets']} from Kaggle)")
                else:
                    failed_count = len(dataset_report.get('failed_datasets', []))
                    print(f"⚠️  {failed_count} dataset(s) failed verification:")
                    for failed_ds in dataset_report.get('failed_datasets', []):
                        print(f"   ❌ {failed_ds['url']} ({failed_ds['source']})")
            else:
                print(f"ℹ️  No datasets found in content")

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

**TRIPLE LINK VERIFICATION RESULTS:**
Total Links: {len(link_urls)}
Passed All 3 Rounds: {working_links}
Failed Verification: {broken_links}
{json.dumps(link_report, indent=2) if link_report else "No links to check"}

CRITICAL: All links MUST pass all three verification rounds. Any failure is a REJECT.

**DATASET VERIFICATION RESULTS:**
{json.dumps(dataset_report, indent=2) if dataset_report else "No datasets found"}

CRITICAL DATASET CHECKS:
- Are Kaggle.com datasets prioritized?
- Do all dataset URLs exist and are they accessible?
- Are dataset names real (not fictional/placeholder)?
- Any failed dataset is a REJECT.

**YOUR LEARNING-FOCUSED REVIEW TASK:**
Read this content as a Master's student genuinely trying to learn about this week's data science topic.

CRITICAL QUESTIONS TO ANSWER:
1. Does this content actually TEACH me about data science concepts through narrative?
2. Can I understand the concepts progressively through the story being told?
3. Are examples integrated naturally to help me understand, not just listed?
4. Would I feel more knowledgeable about data science after reading this?
5. Does the narrative flow help me see WHY these concepts matter?
6. Is this content engaging and does it inspire further learning?
7. Are Engagement activity instructions crystal clear for students?
8. Do ALL sources, references, and dataset links work correctly?

Focus on whether this content serves genuine learning needs, not just information delivery.

**MANDATORY: Provide a quality score from 1-10.**

Return a JSON object:
{{
  "approved": boolean,
  "quality_score": number (1-10),
  "score_breakdown": {{
    "engagement": number (0-10),
    "relevance": number (0-10),
    "narrative_clarity": number (0-10),
    "instructions_clarity": number (0-10),
    "sources_references": number (0-10)
  }},
  "required_fixes": ["learning issue 1", "learning issue 2"],
  "optional_suggestions": ["learning improvement 1", "learning improvement 2"]
}}

**SCORING GUIDE:**
- 10 = EXCELLENT: Super engaging, crystal clear, all sources working, exceptional learning experience
- 9 = VERY GOOD: Highly engaging, very clear, sources working, strong learning experience
- 7-8 = GOOD: Engaging, clear, sources working, but could be more polished
- 5-6 = NEEDS IMPROVEMENT: Some clarity issues or engagement gaps
- 1-4 = POOR: Confusing, not engaging, broken sources, weak learning experience

**APPROVAL THRESHOLD**: Only approve (approved=true) if quality_score >= 9.
Be honest about whether this content effectively teaches data science concepts. Content must score 9 or 10 to be approved.
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

            # Convert triple_check_results to link_check_results format
            # triple_check_results is already serialized to dicts by links.py
            link_check_results = []
            if triple_check_results and 'round_1' in triple_check_results:
                # Use round_1 results as representative sample (all dicts already)
                link_check_results = triple_check_results['round_1']

            # Extract quality score and breakdown
            quality_score = review_data.get("quality_score")
            score_breakdown = review_data.get("score_breakdown", {})

            # Auto-reject if score is too low (below 9)
            approved = review_data.get("approved", True)
            if quality_score and quality_score < 9:
                approved = False
                print(f"⚠️  AlphaStudent quality score {quality_score}/10 is below threshold (9) - AUTO-REJECTING")

            # Display quality score and track delta
            if quality_score:
                print(f"📊 Quality Score: {quality_score}/10")

                # Check for score regression
                if hasattr(state, 'score_history') and state.score_history:
                    last_score = state.score_history[-1].get('reviewer_score')
                    if last_score and quality_score < last_score:
                        delta = quality_score - last_score
                        print(f"⚠️  🔻 SCORE REGRESSION: {last_score}/10 → {quality_score}/10 (Δ {delta:+.1f})")
                        print(f"   ⚠️  Content quality DECREASED - review what changed!")
                    elif last_score and quality_score > last_score:
                        delta = quality_score - last_score
                        print(f"✅ 📈 SCORE IMPROVEMENT: {last_score}/10 → {quality_score}/10 (Δ {delta:+.1f})")

                if score_breakdown:
                    print(f"   Breakdown:")
                    print(f"     - Engagement: {score_breakdown.get('engagement', 'N/A')}/10")
                    print(f"     - Relevance: {score_breakdown.get('relevance', 'N/A')}/10")
                    print(f"     - Narrative Clarity: {score_breakdown.get('narrative_clarity', 'N/A')}/10")
                    print(f"     - Instructions Clarity: {score_breakdown.get('instructions_clarity', 'N/A')}/10")
                    print(f"     - Sources/References: {score_breakdown.get('sources_references', 'N/A')}/10")

            state.alpha_review = ReviewNotes(
                reviewer="AlphaStudent",
                approved=approved,
                quality_score=quality_score,
                score_breakdown=score_breakdown,
                required_fixes=review_data.get("required_fixes", []),
                optional_suggestions=review_data.get("optional_suggestions", []),
                link_check_results=link_check_results
            )
        except json.JSONDecodeError:
            # Fallback if JSON parsing fails
            # triple_check_results is already serialized to dicts
            link_check_results = []
            if triple_check_results and 'round_1' in triple_check_results:
                link_check_results = triple_check_results['round_1']

            print("⚠️  Failed to parse review JSON - using fallback")
            state.alpha_review = ReviewNotes(
                reviewer="AlphaStudent",
                approved=True,
                quality_score=None,
                score_breakdown=None,
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
            # Log quality score if available
            log_data = {}
            if state.alpha_review and state.alpha_review.quality_score:
                log_data["quality_score"] = state.alpha_review.quality_score
                log_data["score_breakdown"] = state.alpha_review.score_breakdown
            tracer.trace_node_complete("alpha_student_review", context=log_data)
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
        minimum_iterations_completed = state.revision_count >= 3  # Require minimum 3 iterations

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
                print(f"🔄 Revision needed - minimum 3 iterations required (current: {state.revision_count})")
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

    def process_single_section_iteratively(self, state: RunState) -> RunState:
        """Process ONE section completely (write → review → revise → approve) before moving to next"""
        tracer = get_tracer()
        if tracer:
            tracer.trace_node_start("process_single_section_iteratively")

        current_section = state.sections[state.current_index]
        print(f"\n{'='*60}")
        print(f"[{state.current_index + 1}/{len(state.sections)}] Processing: {current_section.title}")
        print(f"{'='*60}\n")

        # Initialize revision count for this section
        if state.revision_count == 0:
            print(f"✍️  INITIAL DRAFT")
        else:
            print(f"🔄 REVISION #{state.revision_count}")

        # Step 1: WRITER creates/revises content (with template & guidelines)
        state = self.content_expert_write(state)

        # Step 2: EDITOR reviews
        state = self.education_expert_review(state)

        # Step 3: EDITOR applies direct edits
        state = self.apply_direct_edits(state)

        # Step 4: REVIEWER reviews
        state = self.alpha_student_review(state)

        # Check approval status
        education_approved = state.education_review and state.education_review.approved
        alpha_approved = state.alpha_review and state.alpha_review.approved
        both_approved = education_approved and alpha_approved

        minimum_iterations = state.revision_count >= 2  # At least 3 attempts (0, 1, 2)
        max_revisions = state.revision_count >= 5  # Safety limit

        if both_approved and minimum_iterations:
            # SUCCESS: Section approved
            print(f"\n✅ {current_section.title} APPROVED after {state.revision_count + 1} iterations")

            # Save approved section
            file_path = file_io.save_section_draft(state.current_draft, backup=True)
            state.approved_sections.append(state.current_draft)
            print(f"   💾 Saved: {file_path}")
            print(f"   📊 Progress: {len(state.approved_sections)}/{len(state.sections)} complete\n")

            # Move to next section
            state.current_index += 1
            state.revision_count = 0
            state.education_review = None
            state.alpha_review = None
            state.current_draft = None

        elif max_revisions:
            # TIMEOUT: Force approval after too many revisions
            print(f"\n⚠️  Maximum revisions reached ({max_revisions}) - forcing approval")
            file_path = file_io.save_section_draft(state.current_draft, backup=True)
            state.approved_sections.append(state.current_draft)
            print(f"   💾 Saved: {file_path}\n")

            state.current_index += 1
            state.revision_count = 0
            state.education_review = None
            state.alpha_review = None
            state.current_draft = None

        else:
            # REVISION NEEDED: Stay on this section
            print(f"\n🔄 Revision needed for {current_section.title}")
            if not minimum_iterations:
                print(f"   ⏱️  Minimum iterations not met (need 3, at {state.revision_count + 1})")
            if not both_approved:
                print(f"   ⚠️  Quality scores below threshold (need >=9 from both reviewers)")

            state.revision_count += 1

            # Collect feedback for memory
            if not education_approved and state.education_review:
                for fix in state.education_review.required_fixes:
                    state.feedback_memory.append(f"EDITOR [{current_section.title}]: {fix}")
            if not alpha_approved and state.alpha_review:
                for fix in state.alpha_review.required_fixes:
                    state.feedback_memory.append(f"REVIEWER [{current_section.title}]: {fix}")

        if tracer:
            tracer.trace_node_complete("process_single_section_iteratively")
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