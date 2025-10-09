"""
Context Length Management System
Handles token counting and dynamic truncation to prevent LLM context limits
"""

import tiktoken
import re
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass


@dataclass
class ContextLimits:
    """Model-specific context limits and safety margins"""
    total_tokens: int
    safety_margin: int
    reserved_for_response: int

    @property
    def usable_tokens(self) -> int:
        return self.total_tokens - self.safety_margin - self.reserved_for_response


class ContextManager:
    """Manages context length for LLM interactions"""

    # Model context limits (conservative estimates)
    MODEL_LIMITS = {
        "gpt-4o": ContextLimits(total_tokens=128000, safety_margin=5000, reserved_for_response=32000),
        "gpt-4o-mini": ContextLimits(total_tokens=128000, safety_margin=3000, reserved_for_response=32000),
        "gpt-5-mini": ContextLimits(total_tokens=128000, safety_margin=3000, reserved_for_response=32000),
        "gpt-4.1-mini": ContextLimits(total_tokens=128000, safety_margin=3000, reserved_for_response=32000),
        "gpt-4.1": ContextLimits(total_tokens=128000, safety_margin=3000, reserved_for_response=32000),
        "gpt-4": ContextLimits(total_tokens=8192, safety_margin=1000, reserved_for_response=1000),
        "default": ContextLimits(total_tokens=64000, safety_margin=3000, reserved_for_response=32000)
    }

    def __init__(self, model_name: str = "gpt-4o-mini"):
        self.model_name = model_name
        self.limits = self.MODEL_LIMITS.get(model_name, self.MODEL_LIMITS["default"])

        # Initialize tokenizer
        try:
            self.tokenizer = tiktoken.encoding_for_model("gpt-4")  # Use gpt-4 tokenizer as fallback
        except KeyError:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")  # GPT-4 tokenizer

    def count_tokens(self, text: str) -> int:
        """Count tokens in text"""
        try:
            return len(self.tokenizer.encode(text))
        except Exception:
            # Fallback: rough estimation (4 chars per token on average)
            return len(text) // 4

    def prepare_context(
        self,
        system_prompt: str,
        user_content: str,
        previous_sections: Dict[str, str] = None,
        web_results: List[Dict] = None,
        syllabus_content: str = "",
        template_content: str = "",
        guidelines_content: str = ""
    ) -> Tuple[str, str, Dict[str, int]]:
        """
        Prepare context with dynamic truncation to fit model limits
        Returns: (truncated_system_prompt, truncated_user_content, token_usage)
        """
        # Count base tokens
        system_tokens = self.count_tokens(system_prompt)

        # Build context components with priorities
        context_components = self._build_context_components(
            user_content=user_content,
            previous_sections=previous_sections or {},
            web_results=web_results or [],
            syllabus_content=syllabus_content,
            template_content=template_content,
            guidelines_content=guidelines_content
        )

        # Calculate available tokens for user content
        available_tokens = self.limits.usable_tokens - system_tokens

        # Truncate components to fit within limits
        final_components, token_usage = self._truncate_components(context_components, available_tokens)

        # Build final user content
        final_user_content = self._assemble_final_content(final_components)

        token_usage.update({
            "system_tokens": system_tokens,
            "total_tokens": system_tokens + self.count_tokens(final_user_content),
            "limit": self.limits.usable_tokens
        })

        return system_prompt, final_user_content, token_usage

    def _build_context_components(
        self,
        user_content: str,
        previous_sections: Dict[str, str],
        web_results: List[Dict],
        syllabus_content: str,
        template_content: str,
        guidelines_content: str
    ) -> List[Dict]:
        """Build prioritized context components"""

        components = [
            {
                "name": "user_content",
                "content": user_content,
                "priority": 1,  # Highest priority - never truncate
                "min_tokens": self.count_tokens(user_content),
                "tokens": self.count_tokens(user_content)
            }
        ]

        # Previous sections (summarized if too long)
        if previous_sections:
            prev_content = self._summarize_previous_sections(previous_sections)
            components.append({
                "name": "previous_sections",
                "content": prev_content,
                "priority": 2,
                "min_tokens": 200,  # At least keep some context
                "tokens": self.count_tokens(prev_content)
            })

        # Web results (most recent/relevant first)
        if web_results:
            web_content = self._format_web_results(web_results)
            components.append({
                "name": "web_results",
                "content": web_content,
                "priority": 3,
                "min_tokens": 300,
                "tokens": self.count_tokens(web_content)
            })

        # Guidelines (essential for quality)
        if guidelines_content:
            guidelines_summary = self._extract_key_guidelines(guidelines_content)
            components.append({
                "name": "guidelines",
                "content": guidelines_summary,
                "priority": 4,
                "min_tokens": 400,
                "tokens": self.count_tokens(guidelines_summary)
            })

        # Syllabus content (extract relevant WLOs)
        if syllabus_content:
            syllabus_excerpt = self._extract_relevant_syllabus(syllabus_content)
            components.append({
                "name": "syllabus",
                "content": syllabus_excerpt,
                "priority": 5,
                "min_tokens": 200,
                "tokens": self.count_tokens(syllabus_excerpt)
            })

        # Template content (structural requirements)
        if template_content:
            template_excerpt = self._extract_template_essentials(template_content)
            components.append({
                "name": "template",
                "content": template_excerpt,
                "priority": 6,
                "min_tokens": 150,
                "tokens": self.count_tokens(template_excerpt)
            })

        return components

    def _truncate_components(self, components: List[Dict], available_tokens: int) -> Tuple[List[Dict], Dict[str, int]]:
        """Intelligently truncate components to fit token limit"""

        # Sort by priority
        components.sort(key=lambda x: x["priority"])

        total_tokens = sum(c["tokens"] for c in components)
        token_usage = {"original_total": total_tokens}

        if total_tokens <= available_tokens:
            token_usage["truncated_total"] = total_tokens
            token_usage["truncation_applied"] = False
            return components, token_usage

        # Need to truncate - start with lowest priority items
        final_components = []
        used_tokens = 0

        # First pass: include all priority 1 items (never truncate)
        for component in components:
            if component["priority"] == 1:
                final_components.append(component)
                used_tokens += component["tokens"]

        # Second pass: fit remaining components
        remaining_tokens = available_tokens - used_tokens

        for component in components:
            if component["priority"] == 1:
                continue  # Already included

            if remaining_tokens >= component["min_tokens"]:
                if remaining_tokens >= component["tokens"]:
                    # Include full component
                    final_components.append(component)
                    remaining_tokens -= component["tokens"]
                else:
                    # Truncate component
                    truncated_content = self._truncate_text(
                        component["content"],
                        remaining_tokens - 50  # Leave buffer for truncation notice
                    )
                    truncated_component = component.copy()
                    truncated_component["content"] = truncated_content + "\n\n[Content truncated due to length...]"
                    truncated_component["tokens"] = self.count_tokens(truncated_component["content"])
                    final_components.append(truncated_component)
                    remaining_tokens -= truncated_component["tokens"]

        final_total_tokens = sum(c["tokens"] for c in final_components)
        token_usage.update({
            "truncated_total": final_total_tokens,
            "truncation_applied": True,
            "tokens_saved": total_tokens - final_total_tokens,
            "remaining_budget": available_tokens - final_total_tokens
        })

        return final_components, token_usage

    def _truncate_text(self, text: str, max_tokens: int) -> str:
        """Truncate text to fit within token limit while preserving structure"""
        current_tokens = self.count_tokens(text)

        if current_tokens <= max_tokens:
            return text

        # Estimate characters to keep (rough ratio)
        chars_to_keep = int(len(text) * (max_tokens / current_tokens) * 0.9)  # 10% buffer

        if chars_to_keep < 100:
            return text[:100] + "..."

        truncated = text[:chars_to_keep]

        # Try to truncate at natural boundaries
        for boundary in ["\n\n", "\n", ". ", "? ", "! "]:
            last_boundary = truncated.rfind(boundary)
            if last_boundary > chars_to_keep * 0.8:  # Keep at least 80% of text
                return truncated[:last_boundary + len(boundary)]

        return truncated

    def _summarize_previous_sections(self, previous_sections: Dict[str, str], max_tokens: int = 800) -> str:
        """Create concise summary of previously approved sections"""
        if not previous_sections:
            return "This is the first section of the week."

        summaries = []
        total_tokens = 0

        for section_id, content in previous_sections.items():
            # Extract first paragraph or key points
            lines = content.strip().split('\n')
            summary_lines = []

            for line in lines:
                if line.strip() and not line.startswith('#'):
                    summary_lines.append(line.strip())
                    if len(summary_lines) >= 2:  # Max 2 lines per section
                        break

            if summary_lines:
                section_summary = ' '.join(summary_lines)[:200]  # Max 200 chars per section
                summary_text = f"**{section_id}**: {section_summary}..."

                summary_tokens = self.count_tokens(summary_text)
                if total_tokens + summary_tokens <= max_tokens:
                    summaries.append(summary_text)
                    total_tokens += summary_tokens
                else:
                    break

        if summaries:
            return "**Previously covered:**\n" + '\n'.join(summaries)
        else:
            return "Previous sections have been covered (content summarized due to length)."

    def _format_web_results(self, web_results: List[Dict], max_results: int = 5) -> str:
        """Format web search results for context"""
        if not web_results:
            return ""

        formatted = ["**Fresh Web Sources:**"]

        for i, result in enumerate(web_results[:max_results]):
            title = result.get('title', 'Untitled')
            url = result.get('url', '')
            snippet = result.get('snippet', '')[:150]  # Limit snippet length
            published = result.get('published', '')

            result_text = f"- [{title}]({url})\n  {snippet}..."
            if published:
                result_text += f"\n  Published: {published}"

            formatted.append(result_text)

        return '\n'.join(formatted)

    def summarize_guidelines(self, guidelines_content: str, max_tokens: int = 16000) -> str:
        """
        Intelligently summarize guidelines to fit within token limit.
        Preserves all critical information while condensing verbose sections.
        """
        current_tokens = self.count_tokens(guidelines_content)

        if current_tokens <= max_tokens:
            return guidelines_content

        # Extract structured content by priority
        return self._extract_key_guidelines(guidelines_content, max_tokens=max_tokens)

    def _extract_key_guidelines(self, guidelines_content: str, max_tokens: int = 2000) -> str:
        """
        Extract most important guidelines for the current context.
        Increased max_tokens to 2000 to include more critical information.
        """

        # Key sections to prioritize (ordered by importance)
        priority_sections = [
            "Template Requirements",
            "Building Blocks",
            "Multimedia",
            "Assessment",
            "Citation",
            "WLO",
            "Accessibility",
            "Narrative",
            "Word Count",
            "Structure"
        ]

        lines = guidelines_content.split('\n')
        extracted = []
        current_section = ""
        current_section_content = []
        include_section = False

        for line in lines:
            if line.startswith('#'):
                # Save previous section if it was included
                if include_section and current_section:
                    extracted.append(current_section)
                    # Add summarized content (keep only key points)
                    for content_line in current_section_content[:10]:  # Max 10 lines per section
                        if content_line.strip() and not content_line.strip().startswith(('>', '```', '---')):
                            extracted.append(content_line)
                    current_section_content = []

                # Check if this is a priority section
                include_section = any(keyword.lower() in line.lower() for keyword in priority_sections)
                if include_section:
                    current_section = line
                else:
                    current_section = ""
            elif include_section and line.strip():
                current_section_content.append(line)

        # Add last section if included
        if include_section and current_section:
            extracted.append(current_section)
            for content_line in current_section_content[:10]:
                if content_line.strip() and not content_line.strip().startswith(('>', '```', '---')):
                    extracted.append(content_line)

        # Join and truncate if needed
        guidelines_text = '\n'.join(extracted)

        # If still too long, apply more aggressive truncation
        if self.count_tokens(guidelines_text) > max_tokens:
            guidelines_text = self._truncate_text(guidelines_text, max_tokens)

        return guidelines_text

    def _extract_relevant_syllabus(self, syllabus_content: str, max_tokens: int = 600) -> str:
        """Extract relevant WLOs and context from syllabus"""
        # This is a simplified extraction - could be enhanced with week-specific parsing
        lines = syllabus_content.split('\n')
        relevant_lines = []

        for line in lines:
            line_lower = line.lower()
            if any(keyword in line_lower for keyword in ['wlo', 'learning objective', 'outcome', 'clo']):
                relevant_lines.append(line)

        if not relevant_lines:
            # Fallback: take first few paragraphs
            relevant_lines = [line for line in lines[:20] if line.strip()]

        syllabus_text = '\n'.join(relevant_lines)
        return self._truncate_text(syllabus_text, max_tokens)

    def summarize_template(self, template_content: str, max_tokens: int = 16000) -> str:
        """
        Intelligently summarize template to fit within token limit.
        Preserves all structural requirements and section headers.
        """
        current_tokens = self.count_tokens(template_content)

        if current_tokens <= max_tokens:
            return template_content

        # Extract all headers and key structural elements
        lines = template_content.split('\n')
        summarized = []
        in_code_block = False
        current_section = None
        section_content = []

        for line in lines:
            # Track code blocks
            if line.strip().startswith('```'):
                in_code_block = not in_code_block
                continue

            # Skip code block content
            if in_code_block:
                continue

            # Always include headers
            if line.startswith('#'):
                # Save previous section summary if exists
                if current_section and section_content:
                    summarized.append(current_section)
                    # Keep first 3 and last 2 lines of content for each section
                    if len(section_content) > 5:
                        summarized.extend(section_content[:3])
                        summarized.append("... [content summarized] ...")
                        summarized.extend(section_content[-2:])
                    else:
                        summarized.extend(section_content)
                    section_content = []

                current_section = line
                continue

            # Collect content for current section
            if line.strip():
                # Keep bullet points, numbered lists, and key indicators
                if any(line.strip().startswith(prefix) for prefix in ['- ', '* ', '1.', '2.', '3.', '•', '○']):
                    section_content.append(line)
                # Keep lines with time/duration info
                elif any(keyword in line.lower() for keyword in ['minutes', 'hours', 'time', 'duration', 'wlo', 'required', 'must', 'mandatory']):
                    section_content.append(line)
                # Keep short descriptive lines (likely important)
                elif len(line.strip()) < 100:
                    section_content.append(line)

        # Add last section
        if current_section and section_content:
            summarized.append(current_section)
            if len(section_content) > 5:
                summarized.extend(section_content[:3])
                summarized.append("... [content summarized] ...")
                summarized.extend(section_content[-2:])
            else:
                summarized.extend(section_content)

        result = '\n'.join(summarized)

        # If still too long, use more aggressive truncation
        if self.count_tokens(result) > max_tokens:
            result = self._truncate_text(result, max_tokens)

        return result

    def _extract_template_essentials(self, template_content: str, max_tokens: int = 400) -> str:
        """Extract essential template structure requirements"""
        # Focus on structural elements and requirements
        lines = template_content.split('\n')
        essential_lines = []

        for line in lines:
            line_lower = line.lower().strip()
            # Look for structural indicators
            if (line.strip() and
                (line_lower.startswith(('discovery', 'engagement', 'consolidation')) or
                 'minutes' in line_lower or
                 'wlo' in line_lower or
                 any(keyword in line_lower for keyword in ['required', 'format', 'structure']))):
                essential_lines.append(line)

        if not essential_lines:
            # Fallback: key template indicators
            essential_lines = [line for line in lines if line.strip()][:10]

        template_text = '\n'.join(essential_lines)
        return self._truncate_text(template_text, max_tokens)

    def _assemble_final_content(self, components: List[Dict]) -> str:
        """Assemble final user content from components"""
        content_parts = []

        # Sort by priority to maintain logical order
        components.sort(key=lambda x: x["priority"])

        for component in components:
            if component["name"] == "user_content":
                content_parts.append(component["content"])
            elif component["name"] == "previous_sections":
                content_parts.append(f"\n{component['content']}\n")
            elif component["name"] == "web_results":
                content_parts.append(f"\n{component['content']}\n")
            elif component["name"] == "guidelines":
                content_parts.append(f"\n**Key Guidelines:**\n{component['content']}\n")
            elif component["name"] == "syllabus":
                content_parts.append(f"\n**Syllabus Context:**\n{component['content']}\n")
            elif component["name"] == "template":
                content_parts.append(f"\n**Template Requirements:**\n{component['content']}\n")

        return ''.join(content_parts)

    def get_context_info(self) -> Dict[str, int]:
        """Get current context limits and usage info"""
        return {
            "model": self.model_name,
            "total_limit": self.limits.total_tokens,
            "usable_limit": self.limits.usable_tokens,
            "safety_margin": self.limits.safety_margin,
            "reserved_for_response": self.limits.reserved_for_response
        }