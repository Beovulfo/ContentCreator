from typing import Dict, Any


class PromptTemplates:
    """Minimal prompt templates - each agent only gets what they need"""

    @staticmethod
    def get_section_instruction(section_title: str, section_description: str, week_context: str) -> str:
        """ProgramDirector gives simple section instructions to ContentExpert"""
        return f"""Create content for: {section_title}

Task: {section_description}

Week Context: {week_context}

Write clear, educational content for this section."""

    @staticmethod
    def get_content_expert_system() -> str:
        """ContentExpert - basic writing instructions only, NO template knowledge"""
        return """You are a ContentExpert. Write clear, educational content for data science courses.

Focus on:
- Student-friendly explanations
- Practical examples
- Clear structure
- Academic citations when appropriate

Write good educational content - don't worry about specific formatting."""

    @staticmethod
    def get_education_expert_system() -> str:
        """EducationExpert - template and guidelines compliance"""
        return """You are an EducationExpert. Review content for template compliance and educational standards.

Check:
- Template structure adherence
- Guideline compliance
- Academic quality
- WLO alignment

Provide specific, actionable feedback for fixes needed."""

    @staticmethod
    def get_alpha_student_system() -> str:
        """AlphaStudent - student perspective review"""
        return """You are an AlphaStudent. Review content from a student's perspective.

Focus on:
- Clarity and understandability
- Relevance to the section topic
- Link functionality and appropriateness
- Learning effectiveness
- Remove any irrelevant content

Flag anything confusing or off-topic."""

    # Simplified method signatures for backward compatibility
    @staticmethod
    def get_program_director_request(**kwargs) -> str:
        return PromptTemplates.get_section_instruction(
            kwargs.get('section_title', ''),
            kwargs.get('section_description', ''),
            kwargs.get('week_context', '')
        )

    @staticmethod
    def extract_key_guidelines(guidelines_content: str, section_id: str) -> str:
        """Extract relevant guidelines - simplified"""
        return guidelines_content[:500]  # Just first 500 chars

    @staticmethod
    def check_freshness_needed(description: str, content: str) -> bool:
        """Simple freshness check"""
        keywords = ['latest', 'current', 'recent', 'new', 'modern', '2024', '2025']
        text = (description + content).lower()
        return any(keyword in text for keyword in keywords)