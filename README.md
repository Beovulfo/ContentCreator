# Course Content Creator

A multi-agent system for generating weekly course content for Master's-level Data Science programs. Built with LangGraph and OpenAI, featuring specialized AI agents for content creation, educational review, and student usability testing.

## Features

- **Multi-Agent Workflow**: Four specialized AI agents work together to create high-quality content
- **Web Integration**: Fresh content with current examples, data, and industry insights
- **Quality Assurance**: Dual review process ensuring pedagogical and usability standards
- **Template Compliance**: Strict adherence to course templates and guidelines
- **Link Validation**: Automated checking of all URLs and references
- **Flexible Configuration**: Customizable sections, prompts, and parameters

## Architecture

### Agents

1. **ProgramDirector**: Orchestrates the workflow and makes final decisions
2. **ContentExpert**: Creates section content with web research capabilities
3. **EducationExpert**: Reviews for template compliance and pedagogical quality
4. **AlphaStudent**: Tests content from the student perspective

### Workflow

```
Plan → Request Section → Write Content → Education Review → Student Review → Approve/Revise → Finalize
```

## Quick Start

### 1. Installation

```bash
# Clone or set up the project
cd CourseContentCreator

# Install dependencies
pip install -r requirements.txt
```

### 2. Configuration

```bash
# Copy example secrets file
cp .secrets.example .secrets

# Edit .secrets with your API keys
nano .secrets
```

**Required API Keys:**

**For AI Agents (choose one):**
- **Azure OpenAI** (Primary):
  - `AZURE_ENDPOINT`
  - `AZURE_SUBSCRIPTION_KEY`
  - `AZURE_API_VERSION`
  - `AZURE_DEPLOYMENT`
- **Regular OpenAI** (Alternative):
  - `OPENAI_API_KEY`

**For Web Search (choose one):**
- `TAVILY_API_KEY` (recommended)
- `BING_SEARCH_API_KEY`
- `SERPAPI_API_KEY`
- `GOOGLE_CSE_KEY` + `GOOGLE_CSE_ID`

### 3. Input Files

Place these files in the `./input/` directory:

- `course_syllabus.docx`: Course syllabus with weekly learning objectives
- `template.docx`: Document template defining required sections and structure
- `guidelines.md`: Style guide, citation rules, and authoring guidelines

### 4. Generate Content

```bash
# Generate Week 7 content
export WEEK_NUMBER=7
python -m app.main

# Or specify directly
python -m app.main 7

# Dry run to preview
python -m app.main 7 --dry-run
```

## Configuration Files

### Sections Configuration (`config/sections.json`)

Defines the sections to generate and their order:

```json
[
  {
    "id": "01-introduction",
    "title": "Introduction",
    "description": "Course overview and context for the week's topics"
  },
  ...
]
```

### Course Configuration (`config/course_config.yaml`)

Global settings and parameters:

```yaml
course:
  title: "Data Science Master's Program"
  citation_style: "APA"
  max_word_count_per_section: 1500

freshness:
  max_age_days: 730
  keywords_requiring_freshness:
    - "latest"
    - "current"
    - "industry"
    ...
```

## Output Structure

```
temporal_output/          # Individual approved sections
  01-introduction.md
  02-learning-objectives.md
  ...

weekly_content/           # Final compiled weekly files
  Week7.md

run_logs/                 # Execution logs
  week7.jsonl
```

## Advanced Usage

### Custom Sections

```bash
python -m app.main 7 --sections ./my_custom_sections.json
```

### Custom Configuration

```bash
python -m app.main 7 --course-config ./my_course_config.yaml
```

### Environment Variables

```bash
export WEEK_NUMBER=7
export MODEL_CONTENT_EXPERT=gpt-4o
export HTTP_TIMEOUT_SECONDS=30
python -m app.main
```

## Web Search Integration

The ContentExpert agent automatically uses web search when generating content that benefits from current information. Search is triggered by keywords like:

- "latest", "current", "recent"
- "industry", "trends", "benchmark"
- "2024", "2025", "state-of-the-art"

Search results are integrated into content with proper citations including URLs and publication dates.

## Quality Assurance

### EducationExpert Review

- Template structure compliance
- Learning objective alignment
- Assessment criteria validation
- Academic quality standards

### AlphaStudent Review

- Content clarity and flow
- Link accessibility testing
- Student usability perspective
- Reference consistency

## Troubleshooting

### Common Issues

**Missing API Keys**
```
ERROR: OPENAI_API_KEY environment variable is required
```
Solution: Configure API keys in `.secrets` file

**Missing Input Files**
```
ERROR: Missing required input files: ./input/course_syllabus.docx
```
Solution: Add required files to `./input/` directory

**Web Search Failures**
```
WARNING: No web search provider configured
```
Solution: Configure at least one search provider in `.secrets`

### Logs

Check `./run_logs/week{N}.jsonl` for detailed execution logs including:
- Node transitions
- Review decisions
- Error messages
- Web search results

## Development

### Project Structure

```
app/
  agents/           # Prompt templates and agent logic
  models/           # Pydantic schemas
  tools/            # Web search and link checking
  utils/            # File I/O utilities
  workflow/         # LangGraph nodes
  main.py           # CLI entry point

config/             # Configuration files
input/              # Source materials (syllabus, template, guidelines)
temporal_output/    # Individual section files
weekly_content/     # Final weekly files
run_logs/           # Execution logs
```

### Testing

```bash
# Test with dry run
python -m app.main 1 --dry-run

# Test individual components
python -c "from app.tools import web; print(web.search('data science 2024'))"
```

## License

This project is designed for educational content creation in academic institutions.