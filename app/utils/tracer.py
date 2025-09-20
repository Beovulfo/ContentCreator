"""
Continuous tracing system for workflow visibility
"""

import os
import sys
import json
import time
from datetime import datetime
from typing import Dict, Any, Optional, List
from pathlib import Path


class WorkflowTracer:
    """Provides continuous tracing and progress reporting for the workflow"""

    def __init__(self, week_number: int, verbose: bool = True):
        self.week_number = week_number
        self.verbose = verbose
        self.start_time = time.time()
        self.current_step = 0
        self.total_steps = 0
        self.step_history = []
        self.current_section = None
        self.trace_file = Path(f"./run_logs/week{week_number}_trace.jsonl")

        # Ensure trace directory exists
        self.trace_file.parent.mkdir(parents=True, exist_ok=True)

        # Initialize trace file
        self._write_trace({
            "type": "workflow_start",
            "timestamp": datetime.now().isoformat(),
            "week_number": week_number,
            "start_time": self.start_time
        })

    def set_total_steps(self, sections_count: int):
        """Set the total number of steps based on sections"""
        # Steps: validation + (sections * 4: write, education_review, alpha_review, approve) + final_review + finalize
        self.total_steps = 1 + (sections_count * 4) + 1 + 1
        self._trace("steps_calculated", {"total_steps": self.total_steps, "sections_count": sections_count})

    def start_validation(self):
        """Start the input validation phase"""
        self.current_step += 1
        self._trace_step("input_validation", "🔍 Validating inputs and checking requirements...")

    def validation_complete(self, issues_found: List[str] = None):
        """Complete the validation phase"""
        if issues_found:
            self._trace_step_update("validation_issues_found", f"⚠️  Found {len(issues_found)} issues to resolve")
            for issue in issues_found:
                print(f"   • {issue}")
        else:
            self._trace_step_update("validation_passed", "✅ All inputs validated successfully")

    def start_section(self, section_id: str, section_title: str, section_number: int, total_sections: int):
        """Start processing a new section"""
        self.current_section = {"id": section_id, "title": section_title, "number": section_number}
        self._trace_step(
            "section_start",
            f"📝 Section {section_number}/{total_sections}: {section_title}",
            {"section_id": section_id, "section_title": section_title}
        )

    def start_writing(self, is_revision: bool = False, revision_count: int = 0):
        """Start content writing phase"""
        self.current_step += 1
        if is_revision:
            self._trace_step("writing_revision", f"✏️  Revising content (attempt {revision_count + 1})")
        else:
            self._trace_step("writing_initial", "✏️  Creating initial content")

    def writing_complete(self, word_count: int, links_count: int, citations_count: int):
        """Complete writing phase"""
        self._trace_step_update(
            "writing_complete",
            f"✅ Content created: {word_count} words, {links_count} links, {citations_count} citations"
        )

    def start_education_review(self):
        """Start education expert review"""
        self.current_step += 1
        self._trace_step("education_review", "👩‍🏫 Education Expert reviewing for compliance...")

    def education_review_complete(self, approved: bool, fixes_count: int):
        """Complete education expert review"""
        if approved:
            self._trace_step_update("education_approved", "✅ Education Expert approved")
        else:
            self._trace_step_update("education_revision_needed", f"📋 Education Expert requests {fixes_count} fixes")

    def start_alpha_review(self):
        """Start alpha student review"""
        self.current_step += 1
        self._trace_step("alpha_review", "👨‍🎓 Alpha Student reviewing for clarity...")

    def alpha_review_complete(self, approved: bool, fixes_count: int, working_links: int, total_links: int):
        """Complete alpha student review"""
        if approved:
            self._trace_step_update(
                "alpha_approved",
                f"✅ Alpha Student approved ({working_links}/{total_links} links working)"
            )
        else:
            self._trace_step_update(
                "alpha_revision_needed",
                f"📝 Alpha Student requests {fixes_count} improvements"
            )

    def section_approved(self, final_word_count: int):
        """Section has been approved"""
        self.current_step += 1
        self._trace_step(
            "section_approved",
            f"🎉 Section '{self.current_section['title']}' approved ({final_word_count} words)"
        )

    def start_final_review(self, total_sections: int, total_words: int):
        """Start ProgramDirector final review"""
        self.current_step += 1
        self._trace_step(
            "final_review",
            f"🎭 ProgramDirector reviewing complete week ({total_sections} sections, {total_words} words)..."
        )

    def final_review_complete(self, approved: bool, quality_score: int, issues_count: int):
        """Complete final review"""
        if approved:
            self._trace_step_update(
                "final_approved",
                f"✅ ProgramDirector approved (Quality: {quality_score}/10)"
            )
        else:
            self._trace_step_update(
                "final_revision_needed",
                f"📋 ProgramDirector identified {issues_count} issues (Quality: {quality_score}/10)"
            )

    def start_finalization(self):
        """Start final compilation"""
        self.current_step += 1
        self._trace_step("finalizing", "📁 Compiling final weekly content...")

    def workflow_complete(self, final_path: str, total_word_count: int):
        """Workflow has completed successfully"""
        elapsed = time.time() - self.start_time
        self._trace_step_update(
            "workflow_complete",
            f"🎊 Week {self.week_number} completed in {elapsed:.1f}s"
        )
        print(f"   📄 Final file: {final_path}")
        print(f"   📝 Total words: {total_word_count}")

        self._write_trace({
            "type": "workflow_end",
            "timestamp": datetime.now().isoformat(),
            "elapsed_time": elapsed,
            "final_path": final_path,
            "total_word_count": total_word_count,
            "success": True
        })

    def workflow_error(self, error_message: str, node: str = None):
        """Workflow encountered an error"""
        elapsed = time.time() - self.start_time
        self._trace_step_update("workflow_error", f"❌ Error in {node or 'workflow'}: {error_message}")

        self._write_trace({
            "type": "workflow_error",
            "timestamp": datetime.now().isoformat(),
            "elapsed_time": elapsed,
            "error_message": error_message,
            "node": node,
            "success": False
        })

    def trace_node_start(self, node_name: str, context: Dict[str, Any] = None):
        """Trace the start of a workflow node"""
        self._trace(f"node_start_{node_name}", context or {})

    def trace_node_complete(self, node_name: str, context: Dict[str, Any] = None):
        """Trace the completion of a workflow node"""
        self._trace(f"node_complete_{node_name}", context or {})

    def trace_llm_call(self, agent: str, prompt_length: int, response_length: int, duration: float):
        """Trace LLM API calls"""
        self._trace("llm_call", {
            "agent": agent,
            "prompt_tokens": prompt_length // 4,  # Rough estimate
            "response_tokens": response_length // 4,
            "duration": duration
        })

    def _trace_step(self, step_type: str, message: str, context: Dict[str, Any] = None):
        """Trace a major workflow step"""
        progress = f"[{self.current_step}/{self.total_steps}]" if self.total_steps > 0 else f"[{self.current_step}]"

        if self.verbose:
            print(f"{progress} {message}")

        step_data = {
            "type": "step",
            "step_type": step_type,
            "step_number": self.current_step,
            "total_steps": self.total_steps,
            "message": message,
            "timestamp": datetime.now().isoformat(),
            "elapsed": time.time() - self.start_time
        }
        if context:
            step_data.update(context)

        self.step_history.append(step_data)
        self._write_trace(step_data)

    def _trace_step_update(self, step_type: str, message: str):
        """Update the current step with additional information"""
        if self.verbose:
            print(f"     {message}")

        self._trace(f"step_update_{step_type}", {"message": message})

    def _trace(self, event_type: str, context: Dict[str, Any]):
        """Write a trace event"""
        trace_data = {
            "type": event_type,
            "timestamp": datetime.now().isoformat(),
            "elapsed": time.time() - self.start_time,
            "week_number": self.week_number,
            "current_step": self.current_step,
            "current_section": self.current_section
        }
        trace_data.update(context)

        self._write_trace(trace_data)

    def _write_trace(self, data: Dict[str, Any]):
        """Write trace data to file"""
        try:
            with open(self.trace_file, 'a') as f:
                f.write(json.dumps(data) + '\n')
        except Exception as e:
            # Don't let tracing errors break the workflow
            if self.verbose:
                print(f"⚠️  Trace write error: {e}")

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of the workflow execution"""
        return {
            "week_number": self.week_number,
            "total_steps": self.total_steps,
            "completed_steps": self.current_step,
            "elapsed_time": time.time() - self.start_time,
            "step_history_count": len(self.step_history),
            "trace_file": str(self.trace_file)
        }


# Global tracer instance
_tracer: Optional[WorkflowTracer] = None


def initialize_tracer(week_number: int, verbose: bool = True) -> WorkflowTracer:
    """Initialize the global tracer"""
    global _tracer
    _tracer = WorkflowTracer(week_number, verbose)
    return _tracer


def get_tracer() -> Optional[WorkflowTracer]:
    """Get the current tracer instance"""
    return _tracer


def trace_step(step_type: str, message: str, context: Dict[str, Any] = None):
    """Convenience function for tracing steps"""
    if _tracer:
        _tracer._trace_step(step_type, message, context)


def trace_event(event_type: str, context: Dict[str, Any] = None):
    """Convenience function for tracing events"""
    if _tracer:
        _tracer._trace(event_type, context or {})