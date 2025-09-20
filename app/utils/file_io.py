import os
import json
import yaml
from pathlib import Path
from typing import List, Dict, Any, Optional
from docx import Document
from app.models.schemas import SectionSpec, SectionDraft, CourseInputs


class FileIO:
    def __init__(self, base_path: str = "."):
        self.base_path = Path(base_path)
        self.temporal_output_dir = self.base_path / "temporal_output"
        self.weekly_content_dir = self.base_path / "weekly_content"
        self.run_logs_dir = self.base_path / "run_logs"

        # Ensure directories exist
        self.temporal_output_dir.mkdir(exist_ok=True)
        self.weekly_content_dir.mkdir(exist_ok=True)
        self.run_logs_dir.mkdir(exist_ok=True)

    def load_course_inputs(self, week_number: int) -> CourseInputs:
        """Load and validate all required input files"""
        input_dir = self.base_path / "input"

        return CourseInputs(
            week_number=week_number,
            syllabus_path=str(input_dir / "course_syllabus.docx"),
            template_path=str(input_dir / "template.docx"),
            guidelines_path=str(input_dir / "guidelines.md"),
            sections_config_path=str(self.base_path / "config" / "sections.json"),
            course_config_path=str(self.base_path / "config" / "course_config.yaml")
        )

    def load_sections_config(self, sections_config_path: Optional[str] = None) -> List[SectionSpec]:
        """Load section specifications from JSON config"""
        if sections_config_path and os.path.exists(sections_config_path):
            config_path = sections_config_path
        else:
            config_path = self.base_path / "config" / "sections.json"

        if not os.path.exists(config_path):
            # Return default sections if no config exists
            return self._get_default_sections()

        with open(config_path, 'r', encoding='utf-8') as f:
            sections_data = json.load(f)

        sections = []
        for i, section_data in enumerate(sections_data):
            sections.append(SectionSpec(
                id=section_data["id"],
                title=section_data["title"],
                description=section_data["description"],
                ordinal=i + 1,
                constraints=section_data.get("constraints", {})
            ))

        return sections

    def _get_default_sections(self) -> List[SectionSpec]:
        """Return default section specifications"""
        default_sections = [
            {"id": "01-introduction", "title": "Introduction", "description": "Course overview and context"},
            {"id": "02-learning-objectives", "title": "Weekly Learning Objectives", "description": "Learning outcomes"},
            {"id": "03-required-reading", "title": "Required Reading", "description": "Essential materials"},
            {"id": "04-lecture-notes", "title": "Lecture Notes", "description": "Core content"},
            {"id": "05-learning-activities", "title": "Learning Activities", "description": "Exercises"},
            {"id": "06-assessment-rubric", "title": "Assessment & Rubric", "description": "Evaluation criteria"},
            {"id": "07-further-reading", "title": "Further Reading & Links", "description": "Additional resources"},
            {"id": "08-summary", "title": "Summary & Next Steps", "description": "Week recap"}
        ]

        return [
            SectionSpec(
                id=section["id"],
                title=section["title"],
                description=section["description"],
                ordinal=i + 1
            )
            for i, section in enumerate(default_sections)
        ]

    def load_course_config(self, course_config_path: Optional[str] = None) -> Dict[str, Any]:
        """Load course configuration from YAML"""
        if course_config_path and os.path.exists(course_config_path):
            config_path = course_config_path
        else:
            config_path = self.base_path / "config" / "course_config.yaml"

        if not os.path.exists(config_path):
            return self._get_default_config()

        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def _get_default_config(self) -> Dict[str, Any]:
        """Return default course configuration"""
        return {
            "course": {
                "title": "Data Science Master's Program",
                "citation_style": "APA",
                "max_word_count_per_section": 1500
            },
            "freshness": {
                "max_age_days": 730,
                "keywords_requiring_freshness": [
                    "latest", "current", "recent", "new", "2024", "2025",
                    "industry", "trends", "benchmark", "state-of-the-art"
                ]
            },
            "agents": {
                "program_director": {"temperature": 0.3, "max_tokens": 2000},
                "content_expert": {"temperature": 0.7, "max_tokens": 4000},
                "education_expert": {"temperature": 0.3, "max_tokens": 2000},
                "alpha_student": {"temperature": 0.5, "max_tokens": 2000}
            }
        }

    def read_docx_file(self, file_path: str) -> str:
        """Read content from a DOCX file and return as plain text"""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"DOCX file not found: {file_path}")

        doc = Document(file_path)
        content_parts = []

        for paragraph in doc.paragraphs:
            if paragraph.text.strip():
                content_parts.append(paragraph.text.strip())

        return "\n\n".join(content_parts)

    def read_markdown_file(self, file_path: str) -> str:
        """Read content from a markdown file"""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Markdown file not found: {file_path}")

        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()

    def save_section_draft(self, section_draft: SectionDraft, backup: bool = True) -> str:
        """Save a section draft to temporal_output directory"""
        filename = f"{section_draft.section_id}.md"
        file_path = self.temporal_output_dir / filename

        # Create backup if file exists
        if backup and file_path.exists():
            backup_path = file_path.with_suffix('.md.bak')
            if backup_path.exists():
                backup_path.unlink()
            file_path.rename(backup_path)

        # Create YAML front matter
        front_matter = [
            "---",
            f"section_id: {section_draft.section_id}",
            f"word_count: {section_draft.word_count}",
            f"status: approved",
            "---",
            ""
        ]

        content = "\n".join(front_matter) + section_draft.content_md

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)

        return str(file_path)

    def load_approved_sections(self, section_ids: List[str]) -> Dict[str, str]:
        """Load previously approved sections for context"""
        sections = {}

        for section_id in section_ids:
            filename = f"{section_id}.md"
            file_path = self.temporal_output_dir / filename

            if file_path.exists():
                content = self.read_markdown_file(str(file_path))
                # Remove YAML front matter
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        content = parts[2].strip()
                sections[section_id] = content

        return sections

    def compile_weekly_content(self, week_number: int, sections: List[SectionDraft],
                             week_title: str = "") -> str:
        """Compile all approved sections into final weekly markdown file"""
        if not week_title:
            week_title = f"Data Science Week {week_number}"

        # Build the final document
        content_parts = [
            f"# Week {week_number}: {week_title}",
            "",
            "## Table of Contents",
            ""
        ]

        # Generate TOC
        for section in sections:
            section_title = self._extract_title_from_content(section.content_md)
            anchor = self._create_anchor(section_title)
            content_parts.append(f"- [{section_title}](#{anchor})")

        content_parts.extend(["", "---", ""])

        # Add all sections
        all_citations = []
        for section in sections:
            content_parts.append(section.content_md)
            content_parts.append("")
            all_citations.extend(section.citations)

        # Add deduplicated references if any citations exist
        if all_citations:
            unique_citations = list(dict.fromkeys(all_citations))  # Preserve order, remove duplicates
            content_parts.extend([
                "## References",
                ""
            ])
            content_parts.extend(unique_citations)

        final_content = "\n".join(content_parts)

        # Save to weekly_content directory
        filename = f"Week{week_number}.md"
        file_path = self.weekly_content_dir / filename

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(final_content)

        return str(file_path)

    def _extract_title_from_content(self, content: str) -> str:
        """Extract the first heading from markdown content"""
        lines = content.strip().split('\n')
        for line in lines:
            if line.startswith('# ') or line.startswith('## '):
                return line.lstrip('# ').strip()
        return "Untitled Section"

    def _create_anchor(self, title: str) -> str:
        """Create a URL anchor from a title"""
        anchor = title.lower()
        anchor = anchor.replace(' ', '-')
        anchor = ''.join(c for c in anchor if c.isalnum() or c == '-')
        return anchor

    def log_run_state(self, week_number: int, state_data: Dict[str, Any]) -> None:
        """Log run state to JSONL file"""
        log_filename = f"week{week_number}.jsonl"
        log_path = self.run_logs_dir / log_filename

        log_entry = {
            "timestamp": self._get_timestamp(),
            **state_data
        }

        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry) + '\n')

    def _get_timestamp(self) -> str:
        """Get current timestamp in ISO format"""
        from datetime import datetime
        return datetime.now().isoformat()


# Convenience instance
file_io = FileIO()