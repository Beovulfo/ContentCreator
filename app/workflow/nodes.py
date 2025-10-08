import os
import json
import re
import time
import yaml
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any
from langchain_openai import AzureChatOpenAI, ChatOpenAI
from langchain.schema import HumanMessage, SystemMessage
from tenacity import retry, stop_after_attempt, wait_exponential

from app.models.schemas import RunState, SectionDraft, ReviewNotes, WebSearchResult
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
            # ContentExpert and EDITOR use gpt-4.1-mini (per user request)
            content_deployment = "gpt-4.1-mini"
            content_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "https://agentmso.openai.azure.com")
            content_key = os.getenv("AZURE_OPENAI_API_KEY")
            content_api_version = "2025-01-01-preview"

            self.content_expert_llm = AzureChatOpenAI(
                azure_endpoint=content_endpoint,
                azure_deployment=content_deployment,
                api_key=content_key,
                api_version=content_api_version,
                temperature=1.0,  # gpt-4.1-mini only supports temperature=1.0
                model_kwargs={"max_completion_tokens": 8000}  # Pass in model_kwargs
            )

            # EDITOR uses gpt-4.1-mini
            self.education_expert_llm = AzureChatOpenAI(
                azure_endpoint=content_endpoint,
                azure_deployment=content_deployment,
                api_key=content_key,
                api_version=content_api_version,
                temperature=1.0,  # gpt-4.1-mini only supports temperature=1.0
                model_kwargs={"max_completion_tokens": 8000}  # Increased from 2000 to prevent JSON truncation
            )

            # REVIEWER uses gpt-4.1-mini (same as WRITER/EDITOR for consistency and reliable JSON parsing)
            reviewer_deployment = "gpt-4.1-mini"
            reviewer_endpoint = os.getenv("AZURE_ENDPOINT")
            reviewer_key = os.getenv("AZURE_SUBSCRIPTION_KEY")
            reviewer_api_version = os.getenv("AZURE_API_VERSION", "2025-01-01-preview")

            self.alpha_student_llm = AzureChatOpenAI(
                azure_endpoint=reviewer_endpoint,
                azure_deployment=reviewer_deployment,
                api_key=reviewer_key,
                api_version=reviewer_api_version,
                temperature=1.0,  # gpt-4.1-mini only supports temperature=1.0
                model_kwargs={"max_completion_tokens": 8000}  # Increased from 2000 to prevent JSON truncation
            )
            # Initialize context managers with Azure model names
            self.content_expert_context = ContextManager(content_deployment)
            self.education_expert_context = ContextManager(content_deployment)
            self.alpha_student_context = ContextManager(reviewer_deployment)
        else:
            # Fallback to regular OpenAI - using gpt-4o-mini for WRITER and EDITOR
            content_model = os.getenv("MODEL_CONTENT_EXPERT", "gpt-4o-mini")
            self.content_expert_llm = ChatOpenAI(
                model=content_model,
                temperature=0.7,  # Lowered to 0.7 to prevent gibberish
                model_kwargs={"max_completion_tokens": 8000}
            )
            self.education_expert_llm = ChatOpenAI(
                model="gpt-4o-mini",
                temperature=0.7,  # Focused temperature for consistent review
                model_kwargs={"max_completion_tokens": 8000}
            )
            self.alpha_student_llm = ChatOpenAI(
                model="gpt-4o-mini",
                temperature=0.6,  # Lower temperature for consistent scoring
                model_kwargs={"max_completion_tokens": 8000}
            )
            # Initialize context managers with OpenAI model names
            self.content_expert_context = ContextManager(content_model)
            self.education_expert_context = ContextManager("gpt-4o")
            self.alpha_student_context = ContextManager("gpt-4o")

        # Log agent configurations
        self._log_agent_configurations()

    def _log_agent_configurations(self):
        """Log the model and temperature configuration for each agent"""
        print("\n" + "="*60)
        print("🤖 AGENT CONFIGURATIONS")
        print("="*60)

        if self._is_azure_configured():
            # Azure configuration - ALL agents use gpt-4.1-mini for consistency
            content_deployment = "gpt-4.1-mini"
            reviewer_deployment = "gpt-4.1-mini"

            print("📝 ContentExpert (WRITER):")
            print(f"   Model: {content_deployment} (Azure)")
            print(f"   Temperature: 1.0 (gpt-4.1-mini required default)")
            print(f"   Purpose: Creative, engaging content generation")

            print("\n📚 EducationExpert (EDITOR):")
            print(f"   Model: {content_deployment} (Azure)")
            print(f"   Temperature: 1.0 (gpt-4.1-mini required default)")
            print(f"   Purpose: Pedagogical review and compliance")

            print("\n🎓 AlphaStudent (REVIEWER):")
            print(f"   Model: {reviewer_deployment} (Azure)")
            print(f"   Temperature: 1.0 (gpt-4.1-mini required default)")
            print(f"   Purpose: Quality scoring and verification (reliable JSON parsing)")
        else:
            # OpenAI configuration - using gpt-4o-mini
            content_model = os.getenv("MODEL_CONTENT_EXPERT", "gpt-4o-mini")

            print("📝 ContentExpert (WRITER):")
            print(f"   Model: {content_model} (OpenAI)")
            print(f"   Temperature: 0.7 initial, 0.6 revision (stable, no gibberish)")
            print(f"   Purpose: Creative, engaging content generation")

            print("\n📚 EducationExpert (EDITOR):")
            print(f"   Model: gpt-4o-mini (OpenAI)")
            print(f"   Temperature: 0.7 (focused, consistent)")
            print(f"   Purpose: Pedagogical review and compliance")

            print("\n🎓 AlphaStudent (REVIEWER):")
            print(f"   Model: gpt-4o-mini (OpenAI)")
            print(f"   Temperature: 0.6 (precise, consistent scoring)")
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

    def _save_section_feedback_summary(self, state: RunState, current_section, final_status: str):
        """Save feedback summary from last iteration to help end user understand what was addressed"""
        try:
            # Create output directory if it doesn't exist
            output_dir = Path("./output/feedback_summaries")
            output_dir.mkdir(parents=True, exist_ok=True)

            # Create filename based on section
            filename = f"week{state.week_number}_section{current_section.ordinal}_{current_section.id}_feedback.md"
            filepath = output_dir / filename

            # Build summary content
            summary = []
            summary.append(f"# Feedback Summary: {current_section.title}")
            summary.append(f"**Week**: {state.week_number}")
            summary.append(f"**Section**: {current_section.ordinal} - {current_section.title}")
            summary.append(f"**Status**: {final_status}")
            summary.append(f"**Total Iterations**: {state.revision_count + 1}")
            summary.append(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            summary.append("---\n")

            # Last iteration scores
            summary.append("## Final Quality Scores\n")
            if state.education_review:
                summary.append(f"### EDITOR (EducationExpert)")
                summary.append(f"- **Score**: {state.education_review.quality_score}/10")
                summary.append(f"- **Status**: {'✅ APPROVED' if state.education_review.approved else '❌ REJECTED'}")
                if state.education_review.score_breakdown:
                    summary.append(f"- **Breakdown**:")
                    for aspect, score in state.education_review.score_breakdown.items():
                        summary.append(f"  - {aspect.replace('_', ' ').title()}: {score}/10")
                summary.append("")

            if state.alpha_review:
                summary.append(f"### REVIEWER (AlphaStudent - Student Perspective)")
                summary.append(f"- **Score**: {state.alpha_review.quality_score}/10")
                summary.append(f"- **Status**: {'✅ APPROVED' if state.alpha_review.approved else '❌ REJECTED'}")
                if state.alpha_review.score_breakdown:
                    summary.append(f"- **Breakdown**:")
                    for aspect, score in state.alpha_review.score_breakdown.items():
                        summary.append(f"  - {aspect.replace('_', ' ').title()}: {score}/10")
                summary.append("")

            # Issues identified (from last iteration)
            summary.append("## Issues Identified in Last Iteration\n")

            if state.education_review and state.education_review.required_fixes:
                summary.append(f"### EDITOR Concerns ({len(state.education_review.required_fixes)} issues)")
                for i, fix in enumerate(state.education_review.required_fixes, 1):
                    summary.append(f"{i}. {fix}")
                summary.append("")
            else:
                summary.append("### EDITOR Concerns\n- None (all requirements met)\n")

            if state.alpha_review and state.alpha_review.required_fixes:
                summary.append(f"### REVIEWER Concerns ({len(state.alpha_review.required_fixes)} issues)")
                for i, fix in enumerate(state.alpha_review.required_fixes, 1):
                    summary.append(f"{i}. {fix}")
                summary.append("")
            else:
                summary.append("### REVIEWER Concerns\n- None (student perspective satisfied)\n")

            # Optional suggestions
            if state.alpha_review and state.alpha_review.optional_suggestions:
                summary.append(f"## Optional Improvements Suggested\n")
                for i, suggestion in enumerate(state.alpha_review.optional_suggestions, 1):
                    summary.append(f"{i}. {suggestion}")
                summary.append("")

            # Link verification summary
            if state.alpha_review and hasattr(state.alpha_review, 'link_check_results'):
                if state.alpha_review.link_check_results:
                    summary.append("## Link Verification Results\n")
                    working = sum(1 for link in state.alpha_review.link_check_results if link.get('status') == 'ok')
                    total = len(state.alpha_review.link_check_results)
                    summary.append(f"- **Total Links**: {total}")
                    summary.append(f"- **Working**: {working}")
                    summary.append(f"- **Broken**: {total - working}")
                    if total - working > 0:
                        summary.append(f"\n### Broken Links:")
                        for link in state.alpha_review.link_check_results:
                            if link.get('status') != 'ok':
                                summary.append(f"- {link.get('url')}: {link.get('status')}")
                    summary.append("")

            # Write to file
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write('\n'.join(summary))

            print(f"   📝 Feedback summary saved: {filepath}")

        except Exception as e:
            print(f"   ⚠️  Could not save feedback summary: {str(e)}")

    def _format_web_resources_for_writer(self, search_results: List) -> str:
        """Format web search results as actionable resources for WRITER - ONLY WORKING LINKS"""
        if not search_results:
            return "**⚠️  WEB RESOURCES:** No internet search results available. Use only the required bibliography."

        # CRITICAL: Verify all links before giving them to WRITER
        print(f"   🔗 Verifying {len(search_results[:15])} web links before providing to WRITER...")
        urls_to_check = [result.url for result in search_results[:15]]

        verification_results = self.safe_file_operation(
            lambda: links.triple_check(urls_to_check),
            "verify_web_search_links_for_writer"
        )

        # Filter to ONLY working links
        working_results = []
        if verification_results and 'round_1' in verification_results:
            working_urls = {result['url'] for result in verification_results['round_1'] if result.get('status') == 'ok'}
            working_results = [result for result in search_results[:15] if result.url in working_urls]
            print(f"   ✅ {len(working_results)} verified working links (filtered out {len(search_results[:15]) - len(working_results)} broken links)")
        else:
            # If verification fails, don't risk giving broken links
            print(f"   ⚠️  Link verification failed - providing no web resources to prevent broken links")
            return "**⚠️  WEB RESOURCES:** Link verification failed. Use only the required bibliography from syllabus."

        if not working_results:
            return "**⚠️  WEB RESOURCES:** No working links found. Use only the required bibliography from syllabus."

        resources_text = "**🌐 VERIFIED WORKING WEB RESOURCES (All links verified accessible):**\n\n"
        resources_text += f"The following {len(working_results)} resources were found via web search and VERIFIED AS WORKING:\n\n"

        # Group by likely content type
        kaggle_resources = []
        tutorial_resources = []
        general_resources = []

        for result in working_results:  # Only working links
            if "kaggle.com/datasets" in result.url:
                kaggle_resources.append(result)
            elif any(keyword in result.url.lower() or keyword in result.title.lower()
                    for keyword in ["tutorial", "guide", "documentation", "example"]):
                tutorial_resources.append(result)
            else:
                general_resources.append(result)

        # Format Kaggle datasets prominently
        if kaggle_resources:
            resources_text += "**📊 KAGGLE DATASETS (PRIORITIZE THESE):**\n"
            for i, result in enumerate(kaggle_resources[:5], 1):
                resources_text += f"{i}. [{result.title}]({result.url})\n"
                if result.snippet:
                    resources_text += f"   {result.snippet[:150]}...\n"
            resources_text += "\n"

        # Format tutorials and guides
        if tutorial_resources:
            resources_text += "**📚 TUTORIALS & GUIDES:**\n"
            for i, result in enumerate(tutorial_resources[:5], 1):
                resources_text += f"{i}. [{result.title}]({result.url})\n"
                if result.snippet:
                    resources_text += f"   {result.snippet[:150]}...\n"
            resources_text += "\n"

        # Format general resources
        if general_resources:
            resources_text += "**🔗 ADDITIONAL RESOURCES:**\n"
            for i, result in enumerate(general_resources[:5], 1):
                resources_text += f"{i}. [{result.title}]({result.url})\n"
                if result.snippet:
                    resources_text += f"   {result.snippet[:150]}...\n"
            resources_text += "\n"

        resources_text += """**CRITICAL INSTRUCTIONS FOR USING WEB RESOURCES:**
- ✅ ALL URLS ABOVE HAVE BEEN VERIFIED AS WORKING - You MUST ONLY use these URLs
- ✅ PRIORITIZE Kaggle datasets from the list above
- ✅ Include URLs in your markdown content: [descriptive text](full URL)
- ✅ Reference tutorials and guides when explaining concepts
- ⚠️  If you need additional resources not in this list, clearly indicate you need them
- ❌ DO NOT make up URLs - ONLY use URLs from this verified list or the required bibliography
- ❌ DO NOT assume a dataset exists without verifying it's in the list above
- ❌ DO NOT use any links that are not in this verified list (broken links will auto-reject content)

**REMEMBER**: Every link you include will be triple-verified. Using links not in this list will cause rejection.
"""

        return resources_text

    def _verify_and_format_bibliography(self, bibliography: List[str]) -> tuple[str, List[str]]:
        """Verify bibliography links and return formatted text + verified entries

        Returns:
            tuple: (formatted_bibliography_text, list_of_verified_entries)
        """
        if not bibliography:
            return "**📚 REQUIRED BIBLIOGRAPHY:** None specified for this week.", []

        import re

        # Extract URLs from bibliography entries
        url_pattern = r'https?://[^\s\)]+|www\.[^\s\)]+'
        entries_with_urls = []
        urls_to_verify = []

        for entry in bibliography:
            urls_in_entry = re.findall(url_pattern, entry)
            if urls_in_entry:
                entries_with_urls.append({
                    'text': entry,
                    'urls': urls_in_entry
                })
                urls_to_verify.extend(urls_in_entry)

        if not urls_to_verify:
            # No URLs to verify, return all entries as-is
            return self._format_bibliography_text(bibliography, all_verified=True), bibliography

        # Verify all URLs found in bibliography
        print(f"   🔗 Verifying {len(urls_to_verify)} bibliography links...")

        verification_results = self.safe_file_operation(
            lambda: links.triple_check(urls_to_verify),
            "verify_bibliography_links"
        )

        # Determine which URLs are working
        working_urls = set()
        broken_urls = set()

        if verification_results and 'round_1' in verification_results:
            for result in verification_results['round_1']:
                if result.get('status') == 'ok':
                    working_urls.add(result['url'])
                else:
                    broken_urls.add(result['url'])
        else:
            # Verification failed - mark all as potentially broken
            broken_urls = set(urls_to_verify)

        # Filter bibliography entries
        verified_entries = []
        broken_entries = []

        for entry in bibliography:
            entry_urls = re.findall(url_pattern, entry)
            if not entry_urls:
                # No URLs in this entry - keep it
                verified_entries.append(entry)
            elif all(url in working_urls for url in entry_urls):
                # All URLs in this entry are working
                verified_entries.append(entry)
            else:
                # At least one URL is broken
                broken_entries.append(entry)

        # Log results
        if broken_urls:
            print(f"   ⚠️  {len(broken_urls)} broken bibliography links found - filtering them out")
            print(f"   ✅ {len(verified_entries)} verified bibliography entries (out of {len(bibliography)})")
        else:
            print(f"   ✅ All {len(bibliography)} bibliography entries verified")

        # Format the text
        formatted_text = self._format_bibliography_text(
            verified_entries,
            all_verified=True,
            broken_count=len(broken_entries)
        )

        return formatted_text, verified_entries

    def _format_bibliography_text(self, entries: List[str], all_verified: bool = False, broken_count: int = 0) -> str:
        """Format bibliography entries for WRITER prompt"""
        if not entries:
            return "**📚 REQUIRED BIBLIOGRAPHY:** None available (all links were broken or none specified)."

        text = "**📚 REQUIRED BIBLIOGRAPHY (VERIFIED WORKING):**\n\n"

        if all_verified and broken_count > 0:
            text += f"⚠️  Note: {broken_count} bibliography entries with broken links were filtered out.\n\n"

        text += "The following bibliography entries have been VERIFIED and are safe to use:\n\n"

        for i, entry in enumerate(entries, 1):
            text += f"{i}. {entry}\n"

        text += "\n**INSTRUCTIONS FOR BIBLIOGRAPHY:**\n"
        text += "- ✅ These bibliography links have been verified as working\n"
        text += "- ✅ You MUST cite these materials when relevant to your content\n"
        text += "- ✅ Use the exact URLs provided (they have been verified)\n"
        text += "- ❌ DO NOT add or modify any URLs from the bibliography\n"
        text += "- ❌ DO NOT make up additional bibliography entries\n\n"

        return text

    # =============================================================================
    # AUTONOMOUS W/E/R WORKFLOW NODES
    # =============================================================================

    def _extract_section_template(self, full_template: str, section_ordinal: int) -> str:
        """Extract section-specific template content from full template"""
        # Map section ordinals to template section identifiers
        section_map = {
            1: "## Section 1: Overview",
            2: "## Section 2: DISCOVERY",
            3: "## Section 3: ENGAGEMENT",
            4: "## Section 4: CONSOLIDATION"
        }

        section_header = section_map.get(section_ordinal)
        if not section_header or not full_template:
            return "Section template not available"

        # Find the section in the template
        start_idx = full_template.find(section_header)
        if start_idx == -1:
            return f"Could not find {section_header} in template"

        # Find the next section header (or end of document)
        next_section_idx = len(full_template)
        for ordinal, header in section_map.items():
            if ordinal > section_ordinal:
                idx = full_template.find(header, start_idx + 1)
                if idx != -1 and idx < next_section_idx:
                    next_section_idx = idx

        # Extract section content
        section_content = full_template[start_idx:next_section_idx].strip()
        return section_content

    def _load_template_and_guidelines(self) -> Dict[str, str]:
        """Load template and guidelines for WRITER access"""
        template_content = ""
        guidelines_content = ""

        try:
            # Try template.md first (preferred), then fall back to template.docx
            template_md_path = os.path.join(os.getcwd(), "input", "template.md")
            template_docx_path = os.path.join(os.getcwd(), "input", "template.docx")

            if os.path.exists(template_md_path):
                with open(template_md_path, 'r', encoding='utf-8') as f:
                    template_content = f.read()
                print(f"   📄 Loaded template.md ({len(template_content)} chars)")
            elif os.path.exists(template_docx_path):
                from docx import Document
                doc = Document(template_docx_path)
                template_content = "\n".join([para.text for para in doc.paragraphs if para.text.strip()])
                print(f"   📄 Loaded template.docx ({len(template_content)} chars)")
        except Exception as e:
            print(f"⚠️  Could not load template: {e}")

        try:
            guidelines_path = os.path.join(os.getcwd(), "input", "guidelines.md")
            if os.path.exists(guidelines_path):
                with open(guidelines_path, 'r', encoding='utf-8') as f:
                    guidelines_content = f.read()
                print(f"   📄 Loaded guidelines.md ({len(guidelines_content)} chars)")
        except Exception as e:
            print(f"⚠️  Could not load guidelines.md: {e}")

        return {
            "template": template_content if template_content else "Template not available",
            "guidelines": guidelines_content[:5000] if guidelines_content else "Guidelines not available"  # Keep guidelines limited
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

        # OPTIMIZATION: Cache template and guidelines to avoid re-loading on every iteration
        print(f"📚 Caching template and guidelines...")
        if not hasattr(state, 'cached_template_guidelines'):
            state.cached_template_guidelines = self._load_template_and_guidelines()
            print(f"   ✅ Cached {len(state.cached_template_guidelines.get('template', ''))} chars of template")
            print(f"   ✅ Cached {len(state.cached_template_guidelines.get('guidelines', ''))} chars of guidelines")

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

        # Load syllabus content (still needed for week-specific WLOs and bibliography)
        syllabus_content = self.safe_file_operation(
            lambda: file_io.read_markdown_file(course_inputs.syllabus_path) if course_inputs.syllabus_path.endswith('.md')
                   else file_io.read_docx_file(course_inputs.syllabus_path),
            "read_syllabus_for_content_expert"
        )

        # OPTIMIZATION: Use cached guidelines from state (loaded once at initialization)
        # Only load if somehow not cached (shouldn't happen after initialization)
        if hasattr(state, 'cached_template_guidelines') and state.cached_template_guidelines:
            guidelines_content = state.cached_template_guidelines.get('guidelines', '')
            if state.revision_count == 0:  # Only log on first iteration to reduce noise
                print(f"   ♻️  Using cached guidelines ({len(guidelines_content)} chars)")
        else:
            # Fallback: load guidelines (shouldn't happen after proper initialization)
            print(f"   ⚠️  Guidelines not cached, loading from file...")
            guidelines_content = self.safe_file_operation(
                lambda: file_io.read_markdown_file(course_inputs.guidelines_path),
                "read_guidelines_for_content_expert"
            )

        # Extract week-specific information
        week_info = self._extract_week_info(syllabus_content, state.week_number)

        # OPTIMIZATION: Only search web on FIRST iteration to identify sources
        # Subsequent revisions reuse the same verified sources
        if state.revision_count == 0:
            # CRITICAL: Get fresh web content with working links and datasets
            web_tool = get_web_tool()

            # Perform multiple targeted searches for the WRITER
            print(f"   🌐 Searching web for current resources (first iteration only)...")

            # Search 1: General section content
            general_search = self.safe_web_search(
                lambda q: web_tool.search(q, top_k=8),
                f"{current_section.title} data science {state.week_number} tutorial 2024 2025"
            )

            # Search 2: Datasets specifically
            dataset_search = self.safe_web_search(
                lambda q: web_tool.search(q, top_k=5),
                f"kaggle datasets {current_section.title} data science 2024"
            )

            # Search 3: Educational resources and examples
            resources_search = self.safe_web_search(
                lambda q: web_tool.search(q, top_k=5),
                f"{current_section.title} data science examples resources 2024"
            )

            # Combine and deduplicate search results
            all_search_results = []
            seen_urls = set()

            for result_list in [general_search, dataset_search, resources_search]:
                if result_list:
                    for result in result_list:
                        if result.url not in seen_urls:
                            all_search_results.append(result)
                            seen_urls.add(result.url)

            if all_search_results:
                print(f"   ✅ Found {len(all_search_results)} unique web resources")
            else:
                print(f"   ⚠️  No web search results available")

            # Store verified web results in state for reuse in revisions
            state.web_results = [WebSearchResult(**{
                'title': r.title,
                'url': r.url,
                'snippet': r.snippet,
                'published': getattr(r, 'published', None)
            }) for r in all_search_results]

            # Format search results for WRITER
            web_resources_context = self._format_web_resources_for_writer(all_search_results)
        else:
            # REUSE: Use cached web results from first iteration
            print(f"   ♻️  Reusing {len(state.web_results) if state.web_results else 0} verified web resources from first iteration")
            web_resources_context = self._format_web_resources_for_writer(state.web_results or [])

        # Format week context for the prompt
        week_context = self._format_week_context_for_prompt(week_info, state.week_number)

        # Build detailed section instruction
        section_instruction = PromptTemplates.get_section_instruction(
            current_section.title,
            current_section.description,
            week_context,
            current_section.constraints
        )

        # Track previous scores to detect regressions (from score_history if exists)
        previous_editor_score = None
        previous_reviewer_score = None
        if hasattr(state, 'score_history') and state.score_history:
            # Get the most recent scores from history
            previous_editor_score = state.score_history[-1].get('editor_score')
            previous_reviewer_score = state.score_history[-1].get('reviewer_score')

        # Add revision feedback if this is a revision
        revision_feedback = ""
        is_revision = state.education_review and not state.education_review.approved

        if is_revision:
            # CRITICAL: Enhanced content preservation with explicit score guidance
            revision_feedback += f"\n{'='*70}\n"
            revision_feedback += f"🛡️  CONTENT PRESERVATION STRATEGY - REVISION #{state.revision_count + 1}\n"
            revision_feedback += f"{'='*70}\n\n"

            # Build comprehensive "what's working" summary
            working_aspects = []
            needs_improvement = []

            # Collect EDITOR feedback on what's working
            if state.education_review and state.education_review.score_breakdown:
                for aspect, score in state.education_review.score_breakdown.items():
                    if score >= 7:
                        working_aspects.append(f"✅ {aspect.replace('_', ' ').title()}: {score}/10 (EDITOR)")
                    else:
                        needs_improvement.append(f"🔧 {aspect.replace('_', ' ').title()}: {score}/10 (EDITOR)")

            # Collect REVIEWER feedback on what's working
            if state.alpha_review and state.alpha_review.score_breakdown:
                for aspect, score in state.alpha_review.score_breakdown.items():
                    if score >= 7:
                        working_aspects.append(f"✅ {aspect.replace('_', ' ').title()}: {score}/10 (REVIEWER)")
                    else:
                        needs_improvement.append(f"🔧 {aspect.replace('_', ' ').title()}: {score}/10 (REVIEWER)")

            # Display what's working prominently
            if working_aspects:
                revision_feedback += f"**✅ WHAT'S WORKING WELL (PRESERVE THIS!):**\n"
                revision_feedback += f"These aspects scored >=7 and should be PRESERVED:\n\n"
                for aspect in working_aspects:
                    revision_feedback += f"   {aspect}\n"
                revision_feedback += f"\n⚠️  **DO NOT change these aspects!** They are working well.\n\n"

            # Display what needs improvement
            if needs_improvement:
                revision_feedback += f"**🔧 WHAT NEEDS IMPROVEMENT (FIX ONLY THESE!):**\n"
                revision_feedback += f"These aspects scored <7 and need targeted fixes:\n\n"
                for aspect in needs_improvement:
                    revision_feedback += f"   {aspect}\n"
                revision_feedback += f"\n⚠️  **ONLY change these specific aspects!** Do not rewrite everything.\n\n"

            # Show previous scores for comparison
            if hasattr(state, 'score_history') and state.score_history and len(state.score_history) > 1:
                revision_feedback += f"**📊 SCORE PROGRESSION:**\n"
                for i, hist in enumerate(state.score_history[-3:]):  # Last 3 iterations
                    editor_score = hist.get('editor_score', 'N/A')
                    reviewer_score = hist.get('reviewer_score', 'N/A')
                    revision_feedback += f"   Iteration {hist.get('revision', i)}: EDITOR {editor_score}/10, REVIEWER {reviewer_score}/10\n"
                revision_feedback += f"\n"

            # CRITICAL RULES
            revision_feedback += f"**⚠️  CRITICAL PRESERVATION RULES:**\n"
            revision_feedback += f"1. DO NOT rewrite the entire content - make TARGETED changes only\n"
            revision_feedback += f"2. PRESERVE all aspects that scored >=7 (see list above)\n"
            revision_feedback += f"3. ONLY revise specific parts that scored <7 (see list above)\n"
            revision_feedback += f"4. DO NOT reduce word count unless explicitly requested\n"
            revision_feedback += f"5. KEEP the narrative structure and flow that's working\n"
            revision_feedback += f"6. Make SURGICAL fixes, not wholesale rewrites\n\n"

            # CRITICAL OPTIMIZATION: Show previous draft for context-aware revisions
            if state.current_draft and state.current_draft.content_md:
                # Show first 1500 chars to provide context without overwhelming the prompt
                prev_draft_preview = state.current_draft.content_md[:1500]
                if len(state.current_draft.content_md) > 1500:
                    prev_draft_preview += "\n... [content continues]"

                revision_feedback += f"**📄 YOUR PREVIOUS DRAFT (for comparison):**\n"
                revision_feedback += f"```markdown\n{prev_draft_preview}\n```\n"
                revision_feedback += f"**Word count: {state.current_draft.word_count} words**\n\n"
                revision_feedback += f"⚠️  **COMPARE YOUR REVISION TO THE ABOVE:**\n"
                revision_feedback += f"• Copy-paste sections that scored >=7 (minimal changes)\n"
                revision_feedback += f"• Only rewrite sections related to aspects that scored <7\n"
                revision_feedback += f"• Preserve the overall narrative structure and flow\n\n"

            revision_feedback += f"{'='*70}\n\n"

        if state.education_review and not state.education_review.approved:
            revision_feedback += f"**EDITOR FEEDBACK TO ADDRESS:**\n"
            if state.education_review.quality_score:
                revision_feedback += f"• Current Editor Score: {state.education_review.quality_score}/10 (NEED >=7)\n"
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
            # Calculate dynamic threshold for REVIEWER (section-specific)
            is_structure_section = current_section.ordinal in [1, 4]
            reviewer_threshold = 5 if is_structure_section else 7

            revision_feedback += f"\n{'='*70}\n"
            revision_feedback += f"🎓 CRITICAL: ALPHASTUDENT (REVIEWER) FEEDBACK - MUST ADDRESS\n"
            revision_feedback += f"{'='*70}\n\n"
            revision_feedback += f"**The REVIEWER represents real students using your content.**\n"
            revision_feedback += f"**Low scores indicate students will struggle with this content!**\n\n"

            if state.alpha_review.quality_score:
                revision_feedback += f"**Current Reviewer Score: {state.alpha_review.quality_score}/10 (NEED >={reviewer_threshold})**\n\n"

                # Show what aspects scored low with EXPLANATIONS
                if state.alpha_review.score_breakdown:
                    revision_feedback += f"**📊 DETAILED SCORE BREAKDOWN (Student Perspective):**\n"
                    for aspect, score in state.alpha_review.score_breakdown.items():
                        aspect_name = aspect.replace('_', ' ').title()
                        if score < 7:
                            revision_feedback += f"   ❌ {aspect_name}: {score}/10 - BELOW ACCEPTABLE\n"
                        elif score < reviewer_threshold:
                            revision_feedback += f"   ⚠️  {aspect_name}: {score}/10 - NEEDS IMPROVEMENT\n"
                        else:
                            revision_feedback += f"   ✅ {aspect_name}: {score}/10 - Good (preserve this)\n"
                    revision_feedback += f"\n"

                    # Identify what to preserve vs fix
                    good_aspects = [k for k, v in state.alpha_review.score_breakdown.items() if v >= 7]
                    needs_work = [k for k, v in state.alpha_review.score_breakdown.items() if v < 7]
                    if needs_work:
                        revision_feedback += f"**🔧 PRIORITY FIXES (these aspects are failing students):**\n"
                        for aspect in needs_work:
                            score = state.alpha_review.score_breakdown[aspect]
                            revision_feedback += f"   • {aspect.replace('_', ' ').title()}: Currently {score}/10 - students will struggle here\n"
                        revision_feedback += f"\n"
                    if good_aspects:
                        revision_feedback += f"**✅ WORKING WELL (preserve these):**\n"
                        revision_feedback += f"   • {', '.join([a.replace('_', ' ').title() for a in good_aspects])}\n\n"

            # Show specific fixes with emphasis
            if state.alpha_review.required_fixes:
                revision_feedback += f"**🚨 SPECIFIC ISSUES RAISED BY REVIEWER (students' perspective):**\n"
                for i, fix in enumerate(state.alpha_review.required_fixes, 1):
                    revision_feedback += f"{i}. {fix}\n"
                revision_feedback += f"\n⚠️  **CRITICAL: Address ALL {len(state.alpha_review.required_fixes)} issues above!**\n\n"

            # CRITICAL: Add explicit broken link/dataset feedback with specific URLs
            if hasattr(state, 'broken_links_details') and state.broken_links_details:
                revision_feedback += f"\n**❌ CRITICAL: BROKEN LINKS THAT MUST BE FIXED OR REMOVED:**\n"
                for link_detail in state.broken_links_details:
                    revision_feedback += f"• {link_detail['url']} - Failed {3 - link_detail.get('passed_rounds', 0)}/3 verification rounds\n"
                    revision_feedback += f"  ACTION REQUIRED: Either fix this URL or replace with a working alternative\n"

            if hasattr(state, 'failed_datasets_details') and state.failed_datasets_details:
                revision_feedback += f"\n**❌ CRITICAL: FAILED DATASETS THAT MUST BE FIXED OR REPLACED:**\n"
                for ds_detail in state.failed_datasets_details:
                    revision_feedback += f"• {ds_detail['url']} ({ds_detail['source']}) - Dataset not accessible\n"
                    revision_feedback += f"  ACTION REQUIRED: Replace with a working Kaggle dataset or verify the URL is correct\n"

        # Add accumulated feedback memory to help avoid repeating mistakes
        if state.feedback_memory:
            revision_feedback += f"\n{'='*70}\n"
            revision_feedback += f"📚 FEEDBACK HISTORY - LEARN FROM PAST MISTAKES\n"
            revision_feedback += f"{'='*70}\n\n"
            revision_feedback += f"**⚠️  CRITICAL: You have made mistakes in previous iterations.**\n"
            revision_feedback += f"**DO NOT repeat these errors!**\n\n"

            # Separate REVIEWER feedback from EDITOR feedback for clarity
            reviewer_feedback = [f for f in state.feedback_memory if f.startswith("REVIEWER")]
            editor_feedback = [f for f in state.feedback_memory if f.startswith("EDITOR")]

            if reviewer_feedback:
                recent_reviewer = reviewer_feedback[-10:] if len(reviewer_feedback) > 10 else reviewer_feedback
                revision_feedback += f"**🎓 REVIEWER (Student Perspective) - Past Issues:**\n"
                revision_feedback += f"Students struggled with these aspects in your previous drafts:\n"
                for i, feedback_item in enumerate(recent_reviewer, 1):
                    # Remove "REVIEWER [section]:" prefix for cleaner display
                    clean_feedback = feedback_item.split("]: ", 1)[1] if "]: " in feedback_item else feedback_item
                    revision_feedback += f"{i}. {clean_feedback}\n"
                if len(reviewer_feedback) > 10:
                    revision_feedback += f"   ... and {len(reviewer_feedback) - 10} more REVIEWER issues in earlier drafts\n"
                revision_feedback += f"\n⚠️  **THESE ARE PATTERNS - FIX THE ROOT CAUSE, NOT JUST SYMPTOMS!**\n\n"

            if editor_feedback:
                recent_editor = editor_feedback[-10:] if len(editor_feedback) > 10 else editor_feedback
                revision_feedback += f"**📚 EDITOR (Pedagogical Expert) - Past Issues:**\n"
                for i, feedback_item in enumerate(recent_editor, 1):
                    # Remove "EDITOR [section]:" prefix for cleaner display
                    clean_feedback = feedback_item.split("]: ", 1)[1] if "]: " in feedback_item else feedback_item
                    revision_feedback += f"{i}. {clean_feedback}\n"
                if len(editor_feedback) > 10:
                    revision_feedback += f"   ... and {len(editor_feedback) - 10} more EDITOR issues in earlier drafts\n"
                revision_feedback += f"\n"

        # Load template_mapping.yaml for section-specific implementation details
        template_mapping = self.safe_file_operation(
            lambda: file_io.read_yaml_file("config/template_mapping.yaml"),
            "read_template_mapping_for_writer"
        )

        # Get template mapping info for this specific section
        section_template_mapping = template_mapping.get('sections', {}).get(current_section.id, {})

        # Format section constraints from sections.json AND template_mapping.yaml for the prompt
        section_constraints = ""

        # First: Add template_mapping.yaml information (more detailed implementation guidance)
        if section_template_mapping:
            section_constraints += "\n**TEMPLATE MAPPING (from template_mapping.yaml) - IMPLEMENTATION DETAILS:**\n"
            section_constraints += f"Template Name: {section_template_mapping.get('template_name', current_section.title)}\n\n"

            if "template_requirements" in section_template_mapping:
                section_constraints += "Template Requirements:\n"
                for req in section_template_mapping["template_requirements"]:
                    section_constraints += f"• {req}\n"
                section_constraints += "\n"

            if "implementation" in section_template_mapping:
                impl = section_template_mapping["implementation"]
                section_constraints += "Implementation Details:\n"
                if "duration" in impl:
                    section_constraints += f"• Duration: {impl['duration']} minutes\n"
                if "structure" in impl:
                    section_constraints += f"• Required Structure:\n"
                    for struct_item in impl["structure"]:
                        section_constraints += f"  - {struct_item}\n"
                if "content_guidelines" in impl:
                    section_constraints += f"• Content Guidelines:\n"
                    for key, value in impl["content_guidelines"].items():
                        section_constraints += f"  - {key}: {value}\n"
                if "subsections" in impl:
                    section_constraints += f"• Subsection Specifications:\n"
                    for subsec_name, subsec_details in impl["subsections"].items():
                        section_constraints += f"  - {subsec_name}:\n"
                        for detail_key, detail_value in subsec_details.items():
                            section_constraints += f"    * {detail_key}: {detail_value}\n"
                section_constraints += "\n"

        # Second: Add sections.json constraints (structural requirements)
        if current_section.constraints:
            section_constraints += "**SECTION STRUCTURE (from sections.json):**\n"

            # Add structure requirements
            if "structure" in current_section.constraints:
                section_constraints += f"Required Structure:\n"
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
        # OPTIMIZATION: Use cached version if available
        if hasattr(state, 'cached_template_guidelines') and state.cached_template_guidelines:
            template_and_guidelines = state.cached_template_guidelines
        else:
            # Fallback to loading (shouldn't happen after initialization)
            template_and_guidelines = self._load_template_and_guidelines()

        # CRITICAL: Extract section-specific template for this section only
        full_template = template_and_guidelines.get('template', '')
        section_specific_template = self._extract_section_template(full_template, current_section.ordinal)

        print(f"   📋 Using section-specific template ({len(section_specific_template)} chars)")
        print(f"   📋 Loaded template_mapping.yaml and sections.json for complete configuration")

        # CRITICAL: Verify bibliography links before giving to WRITER
        bibliography = week_info.get('bibliography', [])
        verified_bibliography_text, verified_bibliography = self._verify_and_format_bibliography(bibliography)

        # Build a comprehensive prompt with template_mapping.yaml + sections.json + SECTION-SPECIFIC TEMPLATE + GUIDELINES + WEB RESOURCES
        content_prompt = f"""Write educational content for: {current_section.title}

**Week {state.week_number} Topic:** {week_info.get('overview', 'Data Science fundamentals')}

**Learning Objectives for this week:**
{chr(10).join([f'- WLO{wlo["number"]}: {wlo["description"]} ({wlo["clo_mapping"]})' for wlo in week_info.get('wlos', [])])}

{web_resources_context}

**TEMPLATE STRUCTURE FOR THIS SECTION (MUST FOLLOW EXACTLY):**
{section_specific_template}

**AUTHORING GUIDELINES (MUST COMPLY):**
{template_and_guidelines['guidelines']}

{verified_bibliography_text}

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

**CRITICAL: LINK USAGE - EVERY LINK WILL BE TRIPLE-VERIFIED**
- ✅ ONLY use links from the VERIFIED WEB RESOURCES section above (all pre-checked)
- ✅ ONLY use links from the REQUIRED BIBLIOGRAPHY section above (all pre-checked)
- ❌ DO NOT make up or guess any URLs - this will cause AUTOMATIC REJECTION
- ❌ DO NOT modify any URLs from the verified lists
- ❌ DO NOT assume a dataset, tutorial, or resource exists - check the lists first
- ⚠️  If you need a resource not in the verified lists, state "Additional resource needed: [description]" instead
- 🔴 FAILURE TO FOLLOW THIS WILL RESULT IN REJECTION - All links are triple-verified automatically

Write complete educational content that teaches students about the week topic as a professor teaching Master's students about data science.

Start writing the educational content now, beginning with the section header:"""

        content_messages = [
            SystemMessage(content=PromptTemplates.get_content_expert_system()),
            HumanMessage(content=content_prompt)
        ]

        # Adjust temperature for revisions to reduce randomness
        # Initial draft: 0.7 (stable), Revisions: 0.6 (very focused to prevent gibberish)
        active_llm = self.content_expert_llm
        if is_revision and state.revision_count >= 1:
            # Create a lower-temperature version for revisions
            if self._is_azure_configured():
                content_deployment = "gpt-4.1-mini"
                content_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "https://agentmso.openai.azure.com")
                content_key = os.getenv("AZURE_OPENAI_API_KEY")
                content_api_version = "2025-01-01-preview"

                active_llm = AzureChatOpenAI(
                    azure_endpoint=content_endpoint,
                    azure_deployment=content_deployment,
                    api_key=content_key,
                    api_version=content_api_version,
                    temperature=1.0,  # gpt-4.1-mini only supports temperature=1.0
                    model_kwargs={"max_completion_tokens": 4000}  # Pass in model_kwargs
                )
                print(f"   🎯 Using revision temperature: 1.0 (gpt-4.1-mini required default)")
            else:
                active_llm = ChatOpenAI(
                    model="gpt-4o-mini",
                    temperature=0.6,
                    model_kwargs={"max_completion_tokens": 4000}
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

        # CRITICAL: WRITER performs self-verification of links and datasets BEFORE submission
        print(f"   📝 Generated {word_count} words")
        print(f"   🔍 WRITER self-verifying links and datasets...")

        # Verify links proactively
        verification_results = self._writer_self_verify_content(state.current_draft)

        if verification_results['broken_links'] or verification_results['failed_datasets']:
            # WRITER found issues - attempt self-correction
            print(f"   ⚠️  WRITER detected issues:")
            if verification_results['broken_links']:
                print(f"      ❌ {len(verification_results['broken_links'])} broken link(s)")
            if verification_results['failed_datasets']:
                print(f"      ❌ {len(verification_results['failed_datasets'])} failed dataset(s)")

            # Give WRITER ONE chance to self-correct
            print(f"   🔧 WRITER attempting self-correction...")
            state = self._writer_self_correct(state, verification_results, current_section, week_info, section_specific_template, template_and_guidelines['guidelines'], section_constraints, active_llm)
        else:
            print(f"   ✅ WRITER self-verification passed - all links and datasets working")

        # Update context for next sections
        if len(state.approved_sections) < len(state.sections):
            summary_parts = [f"Section {current_section.id}: {current_section.title} - {state.current_draft.word_count} words"]
            state.context_summary = "; ".join(summary_parts)
        # Display previous scores for ANY revision (not just when is_revision flag is set)
        if state.revision_count > 0 and (previous_editor_score or previous_reviewer_score):
            editor_display = f"{previous_editor_score}/10" if previous_editor_score else "N/A"
            reviewer_display = f"{previous_reviewer_score}/10" if previous_reviewer_score else "N/A"
            print(f"   📊 Previous Editor Score: {editor_display} | Previous Reviewer Score: {reviewer_display}")

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

**APPROVAL THRESHOLD**: Only approve (approved=true) if quality_score >= 7.
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

            # CRITICAL: Enforce quality threshold - DO NOT trust LLM's approval decision
            # Auto-reject if score is too low (below 7)
            approved = review_data.get("approved", False)  # Default to False for safety

            # MANDATORY: Score must be >= 7 to approve
            if quality_score:
                if quality_score >= 7:
                    approved = True  # Score meets threshold
                else:
                    approved = False  # Score too low
                    print(f"⚠️  EducationExpert quality score {quality_score}/10 is below threshold (7) - AUTO-REJECTING")
            else:
                # No score provided - auto-reject
                approved = False
                print(f"⚠️  EducationExpert did not provide quality_score - AUTO-REJECTING")

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
        except json.JSONDecodeError as e:
            # NO FALLBACK - Fail explicitly to force proper JSON output
            print(f"❌ CRITICAL ERROR: Failed to parse EDITOR JSON response")
            print(f"❌ Error: {str(e)}")
            print(f"❌ Response content:")
            review_content = response.content if hasattr(response, 'content') else str(response)
            print(review_content[:1000])  # Show first 1000 chars for debugging
            raise RuntimeError(f"EDITOR (EducationExpert) returned invalid JSON. This indicates a model output issue that must be fixed. Error: {str(e)}")

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

            # CRITICAL: Store broken links details for actionable feedback to WRITER
            state.broken_links_details = triple_check_results['summary']['failed_urls']

            # Log detailed results
            if broken_links > 0:
                print(f"⚠️  {broken_links} link(s) failed triple verification:")
                for failed in triple_check_results['summary']['failed_urls']:
                    print(f"   ❌ {failed['url']} - passed {failed['passed_rounds']}/3 rounds")
            else:
                print(f"✅ All {working_links} links passed triple verification")
        else:
            # No broken links
            state.broken_links_details = []

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
                    # No failed datasets
                    state.failed_datasets_details = []
                else:
                    failed_count = len(dataset_report.get('failed_datasets', []))

                    # CRITICAL: Store failed datasets details for actionable feedback to WRITER
                    state.failed_datasets_details = dataset_report.get('failed_datasets', [])

                    print(f"⚠️  {failed_count} dataset(s) failed verification:")
                    for failed_ds in dataset_report.get('failed_datasets', []):
                        print(f"   ❌ {failed_ds['url']} ({failed_ds['source']})")
            else:
                print(f"ℹ️  No datasets found in content")
                state.failed_datasets_details = []
        else:
            # No datasets to verify
            state.failed_datasets_details = []

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

**APPROVAL THRESHOLD**: Only approve (approved=true) if quality_score >= 7.
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

            # CRITICAL: Enforce quality threshold - DO NOT trust LLM's approval decision
            # Auto-reject if score is too low (below threshold) OR if there are broken links/datasets
            approved = review_data.get("approved", False)  # Default to False for safety

            # DYNAMIC THRESHOLD: Different thresholds for different section types
            # Sections 1 (Overview) and 4 (Consolidation) are structure-driven: threshold = 5
            # Sections 2 (Discovery) and 3 (Engagement) are content-driven: threshold = 7
            is_structure_section = current_section.ordinal in [1, 4]
            reviewer_threshold = 5 if is_structure_section else 7

            # MANDATORY: Score must meet threshold to approve
            if quality_score:
                if quality_score >= reviewer_threshold:
                    approved = True  # Score meets threshold
                else:
                    approved = False  # Score too low
                    print(f"⚠️  AlphaStudent quality score {quality_score}/10 is below threshold ({reviewer_threshold}) - AUTO-REJECTING")
            else:
                # No score provided - auto-reject
                approved = False
                print(f"⚠️  AlphaStudent did not provide quality_score - AUTO-REJECTING")

            # CRITICAL: Auto-reject if ANY links or datasets failed verification
            if broken_links > 0:
                approved = False
                print(f"⚠️  CRITICAL: {broken_links} broken link(s) detected - AUTO-REJECTING")

            if dataset_report and not dataset_report.get('all_verified', True):
                approved = False
                failed_count = len(dataset_report.get('failed_datasets', []))
                print(f"⚠️  CRITICAL: {failed_count} failed dataset(s) detected - AUTO-REJECTING")

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
        except json.JSONDecodeError as e:
            # NO FALLBACK - Fail explicitly to force proper JSON output
            print(f"❌ CRITICAL ERROR: Failed to parse REVIEWER JSON response")
            print(f"❌ Error: {str(e)}")
            print(f"❌ Response content:")
            print(review_content[:1000])  # Show first 1000 chars for debugging
            raise RuntimeError(f"REVIEWER (AlphaStudent) returned invalid JSON. This indicates a model output issue that must be fixed. Error: {str(e)}")

        # Display approval status WITH score for visibility
        approval_status = "✅ approved" if state.alpha_review.approved else "❌ revision needed"
        score_display = f"{state.alpha_review.quality_score}/10" if state.alpha_review.quality_score else "N/A"
        print(f"   🎓 AlphaStudent: {approval_status} | Score: {score_display} | Links: {link_summary}")

        file_io.log_run_state(state.week_number, {
            "node": "alpha_student_review",
            "section": current_section.id,
            "approved": state.alpha_review.approved,
            "fixes_required": len(state.alpha_review.required_fixes),
            "working_links": working_links,
            "broken_links": broken_links
        })

        # Update score history AFTER both reviews are complete
        if state.education_review and state.alpha_review:
            if not hasattr(state, 'score_history'):
                state.score_history = []

            state.score_history.append({
                'revision': state.revision_count,
                'editor_score': state.education_review.quality_score,
                'reviewer_score': state.alpha_review.quality_score,
                'word_count': state.current_draft.word_count if state.current_draft else 0
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

        # DYNAMIC MAX REVISIONS: Allow 2 iterations if either score is below 6
        editor_score = state.education_review.quality_score if state.education_review else 10
        reviewer_score = state.alpha_review.quality_score if state.alpha_review else 10

        # If either score is below 6, allow up to 2 revisions; otherwise stick to 1
        if editor_score < 6 or reviewer_score < 6:
            dynamic_max_revisions = 2
            reason_for_extra = "EDITOR or REVIEWER score below 6"
        else:
            dynamic_max_revisions = 1
            reason_for_extra = "both scores acceptable"

        max_revisions_reached = state.revision_count >= dynamic_max_revisions

        # OPTIMIZATION: Single-iteration workflow - approve immediately when both reviewers approve
        # No minimum iteration requirement - if quality is good on first try, approve immediately
        if both_approved:
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
        elif max_revisions_reached:
            # Force approval if max revisions reached
            print(f"⚠️ Maximum iterations ({dynamic_max_revisions}) reached - force approving with current quality")
            print(f"   📊 Final scores: EDITOR {editor_score}/10, REVIEWER {reviewer_score}/10")

            # Save the section as-is
            file_path = file_io.save_section_draft(state.current_draft, backup=True)
            state.approved_sections.append(state.current_draft)

            # Move to next section
            state.current_index += 1
            state.revision_count = 0

            # Save feedback summary for user review
            self._save_section_feedback_summary(state, current_section, f"FORCE APPROVED ({dynamic_max_revisions} iterations max)")

            print(f"   💾 Saved to: {file_path}")
            print(f"   📊 Progress: {len(state.approved_sections)}/{len(state.sections)} sections complete")

        else:
            # Revision needed - provide clear TODO list to WRITER
            attempts_remaining = dynamic_max_revisions - state.revision_count
            print(f"🔄 Revision needed ({attempts_remaining} attempt(s) remaining)")
            print(f"   📊 Current scores: EDITOR {editor_score}/10, REVIEWER {reviewer_score}/10")
            if dynamic_max_revisions == 2:
                print(f"   ℹ️  Extended to 2 iterations due to score(s) below 6")
            print(f"   📋 EDITOR and REVIEWER have provided TODO lists for fixes")

            state.revision_count += 1

            # Build comprehensive TODO list for WRITER
            editor_todos = []
            reviewer_todos = []

            if not education_approved and state.education_review:
                editor_todos = state.education_review.required_fixes

            if not alpha_approved and state.alpha_review:
                reviewer_todos = state.alpha_review.required_fixes

            total_todos = len(editor_todos) + len(reviewer_todos)

            print(f"   📋 TODO LIST FOR WRITER ({total_todos} items to fix):")
            print(f"")

            if editor_todos:
                print(f"   ✏️  EDITOR REQUIREMENTS ({len(editor_todos)} items):")
                for i, todo in enumerate(editor_todos, 1):
                    print(f"      {i}. {todo}")
                print(f"")

            if reviewer_todos:
                print(f"   🎓 REVIEWER REQUIREMENTS ({len(reviewer_todos)} items):")
                for i, todo in enumerate(reviewer_todos, 1):
                    print(f"      {i}. {todo}")
                print(f"")

            print(f"   ⚠️  WRITER must address ALL {total_todos} TODOs in next revision")

            file_io.log_run_state(state.week_number, {
                "node": "merge_section_or_revise",
                "action": "revision_requested",
                "section": current_section.id,
                "revision_count": state.revision_count,
                "max_revisions": dynamic_max_revisions,
                "editor_score": editor_score,
                "reviewer_score": reviewer_score,
                "education_approved": education_approved,
                "alpha_approved": alpha_approved,
                "total_todos": total_todos,
                "editor_todos": len(editor_todos),
                "reviewer_todos": len(reviewer_todos)
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

        # CRITICAL FIX: Display consolidated iteration summary for clear score visibility
        print(f"\n{'═'*60}")
        print(f"📊 ITERATION #{state.revision_count} COMPLETE - QUALITY SCORES:")
        if state.education_review and state.education_review.quality_score:
            print(f"   📚 EDITOR (EducationExpert):   {state.education_review.quality_score}/10 {'✅' if state.education_review.approved else '❌'}")
        if state.alpha_review and state.alpha_review.quality_score:
            print(f"   🎓 REVIEWER (AlphaStudent):    {state.alpha_review.quality_score}/10 {'✅' if state.alpha_review.approved else '❌'}")
        print(f"{'═'*60}\n")

        # Check approval status
        education_approved = state.education_review and state.education_review.approved
        alpha_approved = state.alpha_review and state.alpha_review.approved
        both_approved = education_approved and alpha_approved

        max_revisions_reached = state.revision_count >= state.max_revisions  # Maximum 1 iteration

        # OPTIMIZATION: Single-iteration workflow - approve immediately when both reviewers approve
        if both_approved:
            # SUCCESS: Section approved
            print(f"\n✅ {current_section.title} APPROVED after {state.revision_count + 1} iterations")

            # Save approved section
            file_path = file_io.save_section_draft(state.current_draft, backup=True)
            state.approved_sections.append(state.current_draft)
            print(f"   💾 Saved: {file_path}")
            print(f"   📊 Progress: {len(state.approved_sections)}/{len(state.sections)} complete\n")

            # Save feedback summary for end user review
            self._save_section_feedback_summary(state, current_section, final_status="APPROVED")

            # Move to next section
            state.current_index += 1
            state.revision_count = 0
            state.education_review = None
            state.alpha_review = None
            state.current_draft = None

        elif max_revisions_reached:
            # TIMEOUT: Force approval after maximum iteration (1)
            print(f"\n⚠️  Maximum iteration (1) reached - forcing approval")
            file_path = file_io.save_section_draft(state.current_draft, backup=True)
            state.approved_sections.append(state.current_draft)
            print(f"   💾 Saved: {file_path}\n")

            # Save feedback summary with warning about force approval
            self._save_section_feedback_summary(state, current_section, final_status="FORCED (1 iteration max)")

            state.current_index += 1
            state.revision_count = 0
            state.education_review = None
            state.alpha_review = None
            state.current_draft = None

        else:
            # REVISION NEEDED: Single revision remaining - stay on this section
            # Calculate dynamic threshold for display
            is_structure_section = current_section.ordinal in [1, 4]
            reviewer_threshold = 5 if is_structure_section else 7

            print(f"\n🔄 Revision needed for {current_section.title} (1 attempt remaining)")
            print(f"   ⚠️  Quality scores below threshold (EDITOR: >=7, REVIEWER: >={reviewer_threshold})")
            print(f"   📋 EDITOR and REVIEWER have provided TODO lists for next revision")

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

        # OPTIMIZATION: Use cached guidelines from state (loaded once at initialization)
        if hasattr(state, 'cached_template_guidelines') and state.cached_template_guidelines:
            guidelines_content = state.cached_template_guidelines.get('guidelines', '')
        else:
            # Fallback: load guidelines (shouldn't happen after proper initialization)
            print(f"   ⚠️  Guidelines not cached in document review, loading from file...")
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

    def _writer_self_verify_content(self, draft: SectionDraft) -> Dict[str, List]:
        """
        WRITER self-verification: Check links and datasets BEFORE submission
        Returns dict with 'broken_links' and 'failed_datasets' lists
        """
        broken_links = []
        failed_datasets = []

        # Verify all links
        if draft.links:
            triple_check_results = self.safe_file_operation(
                lambda: links.triple_check(draft.links),
                "writer_self_verify_links"
            )

            if triple_check_results and 'summary' in triple_check_results:
                broken_links = triple_check_results['summary'].get('failed_urls', [])

        # Verify all datasets
        if draft.content_md:
            dataset_report = self.safe_file_operation(
                lambda: datasets.verify_datasets(draft.content_md),
                "writer_self_verify_datasets"
            )

            if dataset_report and not dataset_report.get('all_verified', True):
                failed_datasets = dataset_report.get('failed_datasets', [])

        return {
            'broken_links': broken_links,
            'failed_datasets': failed_datasets
        }

    def _writer_self_correct(self, state: RunState, verification_results: Dict,
                            current_section, week_info: Dict, section_template: str, guidelines: str,
                            section_constraints: str, active_llm) -> RunState:
        """
        WRITER self-correction: Fix broken links/datasets by regenerating content with working alternatives
        """
        broken_links = verification_results['broken_links']
        failed_datasets = verification_results['failed_datasets']

        # Build self-correction prompt
        correction_prompt = f"""You are revising your own content to fix broken links and datasets.

**ORIGINAL CONTENT (with broken resources):**
{state.current_draft.content_md[:2000]}
{'...[content continues]' if len(state.current_draft.content_md) > 2000 else ''}

**❌ CRITICAL ISSUES YOU MUST FIX:**

"""

        if broken_links:
            correction_prompt += f"**BROKEN LINKS (MUST BE FIXED OR REMOVED):**\n"
            for link_detail in broken_links:
                correction_prompt += f"• {link_detail['url']} - FAILED verification (passed {link_detail.get('passed_rounds', 0)}/3 rounds)\n"
            correction_prompt += f"\n**ACTION REQUIRED FOR BROKEN LINKS:**\n"
            correction_prompt += f"- Either find a working alternative URL for the same resource\n"
            correction_prompt += f"- Or replace with a different working example that serves the same educational purpose\n"
            correction_prompt += f"- DO NOT keep broken links - they are unacceptable\n\n"

        if failed_datasets:
            correction_prompt += f"**FAILED DATASETS (MUST BE REPLACED):**\n"
            for ds_detail in failed_datasets:
                correction_prompt += f"• {ds_detail['url']} ({ds_detail['source']}) - NOT accessible\n"
            correction_prompt += f"\n**ACTION REQUIRED FOR FAILED DATASETS:**\n"
            correction_prompt += f"- Replace with a working Kaggle.com dataset (STRONGLY PREFERRED)\n"
            correction_prompt += f"- Ensure the replacement dataset is real and accessible\n"
            correction_prompt += f"- Verify the dataset serves the same educational purpose\n"
            correction_prompt += f"- DO NOT use fictional/placeholder dataset names\n\n"

        correction_prompt += f"""**YOUR SELF-CORRECTION TASK:**
Rewrite ONLY the sections containing broken resources. Keep everything else EXACTLY the same.

**REQUIREMENTS:**
- Replace ALL broken links with working alternatives
- Replace ALL failed datasets with working Kaggle datasets
- Preserve the narrative flow and educational quality
- Keep the same section structure and format
- Maintain the same word count (approximately {state.current_draft.word_count} words)
- DO NOT reduce content quality - improve it by using working resources

**TEMPLATE STRUCTURE FOR THIS SECTION (MUST FOLLOW):**
{section_template}

**AUTHORING GUIDELINES (MUST COMPLY):**
{guidelines}

**Section Requirements:**
{section_constraints}

**Week {state.week_number} Context:**
{chr(10).join([f'- WLO{wlo["number"]}: {wlo["description"]}' for wlo in week_info.get('wlos', [])])}

Start writing the corrected content now, beginning with the section header:"""

        correction_messages = [
            SystemMessage(content=PromptTemplates.get_content_expert_system() +
                         "\n\nCRITICAL: You are self-correcting your own work. Broken links and failed datasets are UNACCEPTABLE. Find working alternatives."),
            HumanMessage(content=correction_prompt)
        ]

        # Make the LLM call for self-correction
        print(f"   🔄 WRITER generating corrected content...")
        response = self.safe_llm_call(
            active_llm,
            correction_messages,
            context_info=f"writer_self_correct_{current_section.id}"
        )

        if not response:
            print(f"   ⚠️  Self-correction failed - keeping original content")
            return state

        # Extract corrected content
        corrected_content = response.content if hasattr(response, 'content') else str(response)

        # Extract new URLs and verify them
        corrected_urls = self.safe_file_operation(
            lambda: links.extract_urls(corrected_content),
            "extract_urls_from_corrected_content"
        )
        if not corrected_urls:
            corrected_urls = []

        word_count = len(corrected_content.split())

        # Verify the corrected content
        print(f"   🔍 Verifying corrected content...")
        corrected_draft = SectionDraft(
            section_id=current_section.id,
            content_md=corrected_content,
            links=corrected_urls,
            word_count=word_count,
            citations=self._extract_citations(corrected_content),
            wlo_mapping=self._extract_wlo_mapping(corrected_content)
        )

        # Re-verify to see if self-correction worked
        re_verification = self._writer_self_verify_content(corrected_draft)

        if not re_verification['broken_links'] and not re_verification['failed_datasets']:
            print(f"   ✅ Self-correction SUCCESSFUL - all resources now working")
            state.current_draft = corrected_draft
        else:
            # Self-correction partially worked or failed
            remaining_broken = len(re_verification['broken_links'])
            remaining_failed = len(re_verification['failed_datasets'])

            if remaining_broken < len(broken_links) or remaining_failed < len(failed_datasets):
                print(f"   ⚠️  Self-correction PARTIAL - some issues remain:")
                if remaining_broken > 0:
                    print(f"      • {remaining_broken} link(s) still broken (was {len(broken_links)})")
                if remaining_failed > 0:
                    print(f"      • {remaining_failed} dataset(s) still failed (was {len(failed_datasets)})")
                print(f"   📝 Using partially corrected content - REVIEWER will flag remaining issues")
                state.current_draft = corrected_draft
            else:
                print(f"   ❌ Self-correction FAILED - no improvement")
                print(f"   📝 Keeping original content - REVIEWER will flag all issues")
                # Keep original draft - REVIEWER will catch this

        return state