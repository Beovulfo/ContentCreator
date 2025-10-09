import os
import sys
import argparse
from typing import Dict, Any
from pathlib import Path
from dotenv import load_dotenv

from langgraph.graph import StateGraph, END

from app.models.schemas import RunState
from app.utils.file_io import file_io
from app.utils.input_validator import validate_inputs
from app.utils.error_handler import create_error_summary
from app.utils.tracer import initialize_tracer, get_tracer
from app.workflow.nodes import WorkflowNodes


class CourseContentGenerator:
    """Main orchestrator for the course content generation workflow"""

    def __init__(self, base_path: str = "."):
        self.base_path = Path(base_path)
        self.workflow_nodes = None
        self.graph = None

    def initialize_after_secrets_loaded(self):
        """Initialize components that require API keys"""
        self.workflow_nodes = WorkflowNodes()
        self.graph = self._build_workflow_graph()

    def _build_workflow_graph(self) -> StateGraph:
        """Build section-by-section W/E/R workflow (complete each section before moving to next)"""
        workflow = StateGraph(RunState)

        # SECTION-BY-SECTION APPROACH (BETTER FOR QUALITY)
        # Process one section completely (write → review → revise → approve) before next section
        workflow.add_node("initialize_workflow", self.workflow_nodes.initialize_workflow)
        workflow.add_node("process_section", self.workflow_nodes.process_single_section_iteratively)
        workflow.add_node("finalize_complete_week", self.workflow_nodes.finalize_complete_week)

        # Set entry point
        workflow.set_entry_point("initialize_workflow")

        # Flow: Initialize → Process Section (loop until approved) → Next Section or Finalize
        workflow.add_edge("initialize_workflow", "process_section")

        # Conditional: continue with current section, move to next, or finalize
        def should_continue_revise_or_finalize(state: RunState) -> str:
            """Determine next step: revise current section, next section, or finalize"""
            # If current section not approved yet, revise it
            if state.current_index < len(state.sections):
                # Check if section just got approved (moved to next index)
                if len(state.approved_sections) < state.current_index + 1:
                    # Still working on current section
                    return "revise_current"
                elif state.current_index < len(state.sections):
                    # Move to next section
                    return "next_section"

            # All sections complete
            return "finalize"

        workflow.add_conditional_edges(
            "process_section",
            should_continue_revise_or_finalize,
            {
                "revise_current": "process_section",  # Loop: revise current section
                "next_section": "process_section",    # Process next section
                "finalize": "finalize_complete_week"  # All done
            }
        )

        workflow.add_edge("finalize_complete_week", END)

        return workflow.compile()

    def generate_week(self, week_number: int, sections_config: str = None,
                     course_config: str = None, dry_run: bool = False, verbose: bool = True) -> Dict[str, Any]:
        """Generate content for a specific week"""

        # Initialize tracer for continuous progress tracking
        tracer = initialize_tracer(week_number, verbose)

        print(f"🎓 Starting autonomous course content generation for Week {week_number}")

        # Load configuration directly - no interactive validation
        sections = file_io.load_sections_config(sections_config)

        print(f"📋 Loaded {len(sections)} sections to generate")

        # Initialize state
        initial_state = RunState(
            week_number=week_number,
            sections=sections,
            current_index=0,
            max_revisions=5,
            batch_revision_count=0
        )

        if dry_run:
            print("🔍 DRY RUN MODE - No actual content will be generated")
            return {
                "week_number": week_number,
                "sections_count": len(sections),
                "dry_run": True
            }

        try:
            print("🚀 Starting workflow execution...")

            # Execute workflow
            final_state = self.graph.invoke(initial_state)

            # Generate summary (final_state is a dict from LangGraph)
            approved_sections = final_state.get("approved_sections", [])
            sections = final_state.get("sections", [])

            result = {
                "week_number": week_number,
                "sections_generated": len(approved_sections),
                "total_sections": len(sections),
                "total_word_count": sum(s.word_count for s in approved_sections),
                "final_file": f"./weekly_content/Week{week_number}.md",
                "success": len(approved_sections) == len(sections)
            }

            # Add error summary to results
            error_summary = create_error_summary()
            result["error_summary"] = error_summary

            if result["success"]:
                tracer.workflow_complete(result["final_file"], result["total_word_count"])
                print(f"✅ SUCCESS! Generated Week {week_number} content")
                print(f"   📄 {result['sections_generated']} sections")
                print(f"   📝 ~{result['total_word_count']} words total")
                print(f"   💾 Saved to: {result['final_file']}")

                # Show error summary if any errors occurred
                if error_summary["total_errors"] > 0:
                    print(f"   ⚠️  Note: {error_summary['total_errors']} errors handled during generation")
                    print("   📋 Check logs for details on fallbacks used")
            else:
                tracer.workflow_error(f"Incomplete generation: {result['sections_generated']}/{result['total_sections']} sections", "workflow_incomplete")
                print(f"⚠️  PARTIAL SUCCESS - {result['sections_generated']}/{result['total_sections']} sections completed")
                print(f"   💥 {error_summary['total_errors']} errors encountered")

            return result

        except Exception as e:
            error_msg = f"❌ ERROR during generation: {str(e)}"
            print(error_msg)
            tracer.workflow_error(str(e), "main_workflow")
            file_io.log_run_state(week_number, {
                "node": "main",
                "action": "error",
                "error": str(e)
            })
            return {
                "week_number": week_number,
                "success": False,
                "error": str(e)
            }



def load_secrets():
    """Load secrets from .secrets file"""
    secrets_file = Path(".secrets")
    if secrets_file.exists():
        load_dotenv(secrets_file)
        print("🔑 Loaded secrets from .secrets file")
    else:
        print("⚠️  No .secrets file found - using environment variables only")
        print("   Copy .secrets.example to .secrets and configure your API keys")

    # Verify at least one search provider is configured
    search_providers = [
        "TAVILY_API_KEY",
        "BING_SEARCH_API_KEY",
        "SERPAPI_API_KEY"
    ]

    has_search_provider = any(os.getenv(key) for key in search_providers)
    google_cse_configured = os.getenv("GOOGLE_CSE_KEY") and os.getenv("GOOGLE_CSE_ID")

    if not (has_search_provider or google_cse_configured):
        print("⚠️  WARNING: No web search provider configured")
        print("   Content generation will work but won't have fresh web sources")
        print("   Configure one of: TAVILY_API_KEY, BING_SEARCH_API_KEY, SERPAPI_API_KEY, or Google CSE")


def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(
        description="Generate weekly course content for Data Science Master's program",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m app.main 7                    # Generate Week 7 content
  python -m app.main 3 --dry-run         # Preview Week 3 generation
  python -m app.main 5 --sections ./my_sections.json

Environment Variables:
  WEEK_NUMBER                Set week number if not provided as argument
  OPENAI_API_KEY            Required for LLM access
  TAVILY_API_KEY            Recommended web search provider

Configuration Files:
  .secrets                  API keys and model settings
  config/sections.json      Section definitions
  config/course_config.yaml Course settings
        """
    )

    parser.add_argument(
        "week_number",
        type=int,
        nargs="?",
        help="Week number to generate (1-16)"
    )

    parser.add_argument(
        "--sections",
        help="Path to sections configuration JSON file"
    )

    parser.add_argument(
        "--course-config",
        help="Path to course configuration YAML file"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be generated without actually creating content"
    )

    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Reduce output verbosity (disable progress tracing)"
    )

    args = parser.parse_args()

    # Load environment and secrets
    load_secrets()

    # Determine week number
    week_number = args.week_number or os.getenv("WEEK_NUMBER")
    if not week_number:
        print("❌ ERROR: Week number must be provided")
        print("   Use: python -m app.main <week_number>")
        print("   Or set: export WEEK_NUMBER=<week_number>")
        sys.exit(1)

    week_number = int(week_number)
    if not (1 <= week_number <= 16):
        print(f"❌ ERROR: Week number must be between 1 and 16, got {week_number}")
        sys.exit(1)

    # Check required API keys (either Azure OpenAI or regular OpenAI)
    azure_configured = all(os.getenv(var) for var in ["AZURE_ENDPOINT", "AZURE_SUBSCRIPTION_KEY", "AZURE_API_VERSION"])
    openai_configured = os.getenv("OPENAI_API_KEY") is not None

    if not (azure_configured or openai_configured):
        print("❌ ERROR: LLM configuration is required")
        print("   Either configure Azure OpenAI:")
        print("     AZURE_ENDPOINT, AZURE_SUBSCRIPTION_KEY, AZURE_API_VERSION")
        print("   Or configure regular OpenAI:")
        print("     OPENAI_API_KEY")
        print("   Set these in .secrets file or environment")
        sys.exit(1)

    if azure_configured:
        print("🔑 Using Azure OpenAI configuration")
        print(f"   Endpoint: {os.getenv('AZURE_ENDPOINT')}")
        print(f"   Default Deployment: {os.getenv('AZURE_DEPLOYMENT', 'gpt-5-mini')}")
        gpt4o_deployment = os.getenv('AZURE_GPT4O_DEPLOYMENT')
        if gpt4o_deployment:
            print(f"   GPT-4o Deployment: {gpt4o_deployment} (for ContentExpert)")
    elif openai_configured:
        print("🔑 Using OpenAI configuration")
        print(f"   ContentExpert: {os.getenv('MODEL_CONTENT_EXPERT', 'gpt-4o')}")
        print(f"   EducationExpert: {os.getenv('MODEL_EDUCATION_EXPERT', 'gpt-4.1')}")
        print(f"   AlphaStudent: {os.getenv('MODEL_ALPHA_STUDENT', 'gpt-4.1')}")
    else:
        print("⚠️  Warning: Unexpected configuration state")

    # Initialize generator and run
    try:
        generator = CourseContentGenerator()
        generator.initialize_after_secrets_loaded()
        result = generator.generate_week(
            week_number=week_number,
            sections_config=args.sections,
            course_config=args.course_config,
            dry_run=args.dry_run,
            verbose=not args.quiet
        )

        if result.get("success"):
            sys.exit(0)
        else:
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n🛑 Generation interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"❌ FATAL ERROR: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()