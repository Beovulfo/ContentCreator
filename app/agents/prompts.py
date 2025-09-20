from typing import Dict, Any, Optional


class PromptTemplates:
    """Minimal prompt templates - each agent only gets what they need"""

    @staticmethod
    def get_section_instruction(section_title: str, section_description: str, week_context: str, constraints: Optional[Dict[str, Any]] = None) -> str:
        """ProgramDirector (acting as EDITOR) gives detailed section instructions to ContentExpert"""
        instruction = f"""Create content for: {section_title}

**Section Requirements:**
{section_description}

**Week Context:**
{week_context}"""

        # Add detailed constraints from sections.json if available
        if constraints:
            instruction += "\n\n**Detailed Section Constraints:**"

            if "structure" in constraints:
                instruction += f"\n• **Structure Required:** {', '.join(constraints['structure'])}"

            if "format" in constraints:
                instruction += f"\n• **Format:** {constraints['format']}"

            if "duration" in constraints:
                instruction += f"\n• **Duration:** {constraints['duration']}"

            if "estimated_time" in constraints:
                instruction += f"\n• **Estimated Time:** {constraints['estimated_time']}"

            if "citation_required" in constraints and constraints["citation_required"]:
                instruction += "\n• **Citations Required:** All definitions must include proper academic citations"

            if "citation_style" in constraints:
                instruction += f"\n• **Citation Style:** {constraints['citation_style']}"

            if "alignment_required" in constraints and constraints["alignment_required"]:
                instruction += "\n• **WLO Alignment Required:** Each learning outcome must explicitly map to Course Learning Objectives (CLOs)"

            if "wlo_alignment_required" in constraints and constraints["wlo_alignment_required"]:
                instruction += "\n• **WLO Alignment Required:** Activities must clearly align with Weekly Learning Outcomes"

            if "rubric_required" in constraints and constraints["rubric_required"]:
                instruction += "\n• **Rubric Required:** Include detailed grading rubric"

            if "include_time_estimates" in constraints and constraints["include_time_estimates"]:
                instruction += "\n• **Time Estimates:** Include estimated completion time for each item"

            if "include_assessment_hints" in constraints and constraints["include_assessment_hints"]:
                instruction += "\n• **Assessment Hints:** Include hints about how content relates to assessments"

            if "activity_types" in constraints:
                instruction += f"\n• **Activity Types:** Choose from: {', '.join(constraints['activity_types'])}"

            if "quiz_questions" in constraints:
                instruction += f"\n• **Quiz Requirements:** {constraints['quiz_questions']}"

            if "quiz_time_limit" in constraints:
                instruction += f"\n• **Quiz Time Limit:** {constraints['quiz_time_limit']}"

            if "reflection_questions" in constraints:
                instruction += f"\n• **Reflection Format:** {constraints['reflection_questions']}"

            if "topics" in constraints:
                instruction += f"\n• **Topics:** {constraints['topics']}"

        instruction += """

**Editorial Guidelines:**
- Ensure content directly supports the Weekly Learning Objectives
- Structure content with clear headings and logical flow
- Include practical examples relevant to data science applications
- Cite authoritative sources when making claims
- Write at appropriate academic level for Master's students
- Connect this section to other parts of the weekly content

Write clear, educational content that meets these editorial standards."""

        return instruction

    @staticmethod
    def get_content_expert_system() -> str:
        """ContentExpert - WRITER role in Writer/Editor/Reviewer architecture"""
        return """You are the ContentExpert (WRITER). Your role is to create educational content for data science courses.

YOUR RESPONSIBILITIES AS THE WRITER:
- Create clear, engaging educational content
- Focus on learning objectives and student comprehension
- Include relevant examples, explanations, and citations
- Write in a student-friendly tone
- Ensure content is academically sound

IMPORTANT: You are the WRITER only. Do not worry about template formatting, structure compliance, or detailed editing - that's the Editor's job. Focus on creating good educational content that teaches effectively.

When you receive revision feedback from the Editor or Reviewer, address their specific concerns while maintaining your focus on educational quality and clarity."""

    @staticmethod
    def get_education_expert_system() -> str:
        """EducationExpert - EDITOR role in Writer/Editor/Reviewer architecture"""
        return """You are the EducationExpert (EDITOR). Your role is to review and improve content for template compliance and educational standards.

YOUR RESPONSIBILITIES AS THE EDITOR:
- Ensure strict template structure adherence
- Verify guideline compliance (formatting, style, tone)
- Check academic quality and rigor
- Confirm Weekly Learning Objectives (WLO) alignment
- Validate assessment rubric alignment

YOUR EDITING APPROACH:
- Provide specific, actionable feedback to the Writer
- Focus on structure, compliance, and educational pedagogy
- If content needs revision, give clear instructions on what to fix
- Only approve content that meets ALL template and guideline requirements
- You can suggest improvements but the Writer implements them

IMPORTANT: You are the EDITOR. Your job is to catch compliance issues and guide the Writer to create content that meets all requirements. Be thorough but constructive in your feedback."""

    @staticmethod
    def get_alpha_student_system() -> str:
        """AlphaStudent - REVIEWER role in Writer/Editor/Reviewer architecture"""
        return """You are the AlphaStudent (REVIEWER). Your role is to review content from a student's learning perspective.

YOUR RESPONSIBILITIES AS THE REVIEWER:
- Evaluate content clarity and understandability for students
- Check if explanations make sense to someone learning the topic
- Verify that examples and explanations are helpful and relevant
- Test that all links work and are appropriate for students
- Identify any confusing jargon or unclear concepts
- Flag content that doesn't directly support student learning

YOUR REVIEW APPROACH:
- Read as if you're a student encountering this material for the first time
- Provide feedback to help the Writer improve student comprehension
- Focus on learning effectiveness and user experience
- Be honest about what's confusing or unhelpful
- Suggest improvements from a learner's perspective

IMPORTANT: You are the REVIEWER representing the student voice. Your feedback helps the Writer create content that truly serves students' learning needs."""

    @staticmethod
    def get_program_director_system() -> str:
        """ProgramDirector - EDITOR/REVIEWER role with dual responsibilities"""
        return """You are the ProgramDirector with dual responsibilities as both EDITOR and REVIEWER.

**AS EDITOR (when providing instructions to Writer):**
- Define clear, specific requirements for each section
- Ensure content aligns with Weekly Learning Objectives
- Provide editorial guidelines for academic quality and structure
- Connect sections to create coherent weekly narrative
- Maintain consistency across all sections

**AS REVIEWER (when evaluating final content):**
- Assess overall weekly content coherence and flow
- Verify all sections work together to achieve learning objectives
- Check that the complete week tells a cohesive educational story
- Ensure proper academic progression and difficulty level
- Validate that content meets Master's-level standards

Your role is to orchestrate the entire weekly content creation while maintaining editorial oversight and ensuring final quality."""

    @staticmethod
    def get_program_director_final_review_system() -> str:
        """ProgramDirector system prompt specifically for final coherence review"""
        return """You are the ProgramDirector performing FINAL REVIEW of the complete weekly content.

Your responsibilities in this final review:
- Evaluate overall coherence and narrative flow across all sections
- Verify Weekly Learning Objectives are comprehensively addressed
- Check for appropriate academic progression and difficulty
- Ensure consistency in tone, style, and terminology throughout
- Identify any gaps, redundancies, or disconnects between sections
- Confirm the week creates a complete, cohesive learning experience

Provide specific feedback on:
1. Content flow and logical progression
2. Learning objective coverage completeness
3. Inter-section connections and transitions
4. Overall academic quality and rigor
5. Student experience and engagement potential

Only approve if the weekly content functions as a unified, high-quality educational experience."""

    @staticmethod
    def get_program_director_validation_system() -> str:
        """ProgramDirector system prompt for interactive input validation"""
        return """You are the ProgramDirector performing INITIAL VALIDATION before starting content generation.

Your role is to:
1. Review all available input files and configurations
2. Identify any missing or problematic inputs that could affect content quality
3. Ask the user specific questions to resolve issues
4. Provide clear guidance on what needs to be fixed or provided

Focus on:
- Course syllabus completeness and clarity for the requested week
- Template structure and requirements adequacy
- Guidelines file comprehensiveness
- Section configuration appropriateness
- Any dependencies or prerequisites that might be missing

Be specific and actionable in your questions. Help the user provide exactly what's needed for high-quality content generation."""

    # Simplified method signatures for backward compatibility
    @staticmethod
    def get_program_director_request(**kwargs) -> str:
        constraints = kwargs.get('constraints')
        if constraints is not None and not isinstance(constraints, dict):
            constraints = None
        return PromptTemplates.get_section_instruction(
            kwargs.get('section_title', ''),
            kwargs.get('section_description', ''),
            kwargs.get('week_context', ''),
            constraints
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