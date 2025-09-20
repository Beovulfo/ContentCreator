"""
Error Handling and Graceful Degradation System
Provides robust error handling and recovery mechanisms
"""

import logging
import traceback
from typing import Optional, Dict, Any, Callable, TypeVar, Union
from functools import wraps
from dataclasses import dataclass
from enum import Enum


class ErrorSeverity(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ErrorContext:
    """Context information for error handling"""
    operation: str
    component: str
    attempt: int
    max_attempts: int
    fallback_available: bool
    user_message: str
    technical_details: str


class GracefulErrorHandler:
    """Centralized error handling with graceful degradation"""

    def __init__(self, log_file: str = "error_log.txt"):
        self.logger = self._setup_logger(log_file)
        self.error_counts = {}
        self.fallback_strategies = {}

    def _setup_logger(self, log_file: str) -> logging.Logger:
        """Setup logging for error tracking"""
        logger = logging.getLogger("CourseContentGenerator")
        logger.setLevel(logging.INFO)

        if not logger.handlers:
            # File handler
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(logging.INFO)

            # Console handler
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.WARNING)

            # Formatter
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            file_handler.setFormatter(formatter)
            console_handler.setFormatter(formatter)

            logger.addHandler(file_handler)
            logger.addHandler(console_handler)

        return logger

    def register_fallback(self, operation: str, fallback_func: Callable):
        """Register a fallback strategy for an operation"""
        self.fallback_strategies[operation] = fallback_func

    def handle_error(
        self,
        error: Exception,
        context: ErrorContext,
        severity: ErrorSeverity = ErrorSeverity.MEDIUM
    ) -> Dict[str, Any]:
        """
        Handle an error with appropriate severity and fallback
        Returns: {'success': bool, 'result': Any, 'fallback_used': bool, 'message': str}
        """

        # Increment error count
        error_key = f"{context.component}.{context.operation}"
        self.error_counts[error_key] = self.error_counts.get(error_key, 0) + 1

        # Log the error
        self._log_error(error, context, severity)

        # Determine response strategy
        response = {
            'success': False,
            'result': None,
            'fallback_used': False,
            'message': context.user_message,
            'technical_details': str(error)
        }

        # Try fallback if available and appropriate
        if context.fallback_available and severity != ErrorSeverity.CRITICAL:
            fallback_key = f"{context.component}.{context.operation}"
            if fallback_key in self.fallback_strategies:
                try:
                    self.logger.info(f"Attempting fallback for {fallback_key}")
                    fallback_result = self.fallback_strategies[fallback_key]()

                    response.update({
                        'success': True,
                        'result': fallback_result,
                        'fallback_used': True,
                        'message': f"{context.user_message} (using fallback method)"
                    })

                    self.logger.info(f"Fallback successful for {fallback_key}")

                except Exception as fallback_error:
                    self.logger.error(f"Fallback failed for {fallback_key}: {str(fallback_error)}")

        return response

    def _log_error(self, error: Exception, context: ErrorContext, severity: ErrorSeverity):
        """Log error with appropriate level based on severity"""

        error_msg = f"[{context.component}.{context.operation}] {context.technical_details}"

        if severity == ErrorSeverity.CRITICAL:
            self.logger.critical(error_msg)
        elif severity == ErrorSeverity.HIGH:
            self.logger.error(error_msg)
        elif severity == ErrorSeverity.MEDIUM:
            self.logger.warning(error_msg)
        else:
            self.logger.info(error_msg)

        # Log full traceback for debugging
        self.logger.debug(f"Full traceback: {traceback.format_exc()}")

    def get_error_stats(self) -> Dict[str, int]:
        """Get error statistics"""
        return self.error_counts.copy()


# Global error handler instance
error_handler = GracefulErrorHandler()


def with_error_handling(
    component: str,
    operation: str,
    fallback_available: bool = False,
    severity: ErrorSeverity = ErrorSeverity.MEDIUM,
    user_message: str = "An error occurred",
    max_retries: int = 3
):
    """
    Decorator for adding error handling to functions
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e

                    context = ErrorContext(
                        operation=operation,
                        component=component,
                        attempt=attempt + 1,
                        max_attempts=max_retries + 1,
                        fallback_available=fallback_available,
                        user_message=user_message,
                        technical_details=str(e)
                    )

                    # If this is not the last attempt, just log and retry
                    if attempt < max_retries:
                        error_handler.logger.warning(
                            f"Attempt {attempt + 1}/{max_retries + 1} failed for "
                            f"{component}.{operation}: {str(e)}"
                        )
                        continue

                    # Last attempt - handle error properly
                    result = error_handler.handle_error(last_exception, context, severity)

                    if result['success']:
                        return result['result']
                    else:
                        # Re-raise the original exception if no fallback worked
                        raise last_exception

            # Should never reach here, but just in case
            raise last_exception

        return wrapper
    return decorator


class ComponentErrorHandlers:
    """Specific error handlers for different system components"""

    @staticmethod
    def web_search_fallback() -> list:
        """Fallback when web search fails"""
        error_handler.logger.info("Web search failed - continuing without fresh content")
        return []

    @staticmethod
    def docx_parsing_fallback() -> str:
        """Fallback when DOCX parsing fails"""
        error_handler.logger.info("DOCX parsing failed - using minimal content")
        return "Content unavailable due to file parsing error. Please check input files."

    @staticmethod
    def link_check_fallback() -> list:
        """Fallback when link checking fails"""
        error_handler.logger.info("Link validation failed - skipping URL checks")
        return []

    @staticmethod
    def llm_call_fallback() -> str:
        """Fallback when LLM call fails"""
        error_handler.logger.info("LLM call failed - using template response")
        return "Content generation temporarily unavailable. Please try again later."

    @staticmethod
    def file_save_fallback() -> str:
        """Fallback when file saving fails"""
        import tempfile
        import time

        timestamp = int(time.time())
        fallback_path = f"{tempfile.gettempdir()}/course_content_backup_{timestamp}.md"
        error_handler.logger.info(f"File save failed - using backup location: {fallback_path}")
        return fallback_path


# Register default fallbacks
error_handler.register_fallback("web_search.search", ComponentErrorHandlers.web_search_fallback)
error_handler.register_fallback("file_io.read_docx", ComponentErrorHandlers.docx_parsing_fallback)
error_handler.register_fallback("links.check", ComponentErrorHandlers.link_check_fallback)
error_handler.register_fallback("workflow.llm_call", ComponentErrorHandlers.llm_call_fallback)
error_handler.register_fallback("file_io.save", ComponentErrorHandlers.file_save_fallback)


class RobustWorkflowMixin:
    """Mixin class for adding robust error handling to workflow nodes"""

    def safe_llm_call(self, llm, messages, context_info: str = ""):
        """Safely call LLM with error handling and fallback"""
        try:
            return llm.invoke(messages)
        except Exception as e:
            error_context = ErrorContext(
                operation="llm_call",
                component="workflow",
                attempt=1,
                max_attempts=1,
                fallback_available=True,
                user_message=f"AI model call failed for {context_info}",
                technical_details=str(e)
            )

            result = error_handler.handle_error(e, error_context, ErrorSeverity.HIGH)

            if result['success']:
                # Create a mock response object
                class MockResponse:
                    def __init__(self, content):
                        self.content = content

                return MockResponse(result['result'])
            else:
                # Re-raise if no fallback worked
                raise e

    def safe_file_operation(self, operation_func, operation_name: str):
        """Safely perform file operations with error handling"""
        try:
            return operation_func()
        except Exception as e:
            error_context = ErrorContext(
                operation=operation_name,
                component="file_io",
                attempt=1,
                max_attempts=1,
                fallback_available=True,
                user_message=f"File operation failed: {operation_name}",
                technical_details=str(e)
            )

            result = error_handler.handle_error(e, error_context, ErrorSeverity.MEDIUM)

            if result['success']:
                return result['result']
            else:
                raise e

    def safe_web_search(self, search_func, query: str):
        """Safely perform web search with fallback"""
        try:
            return search_func(query)
        except Exception as e:
            error_context = ErrorContext(
                operation="search",
                component="web_search",
                attempt=1,
                max_attempts=1,
                fallback_available=True,
                user_message=f"Web search failed for query: {query}",
                technical_details=str(e)
            )

            result = error_handler.handle_error(e, error_context, ErrorSeverity.LOW)
            return result.get('result', [])  # Return empty list if no results


def create_error_summary() -> Dict[str, Any]:
    """Create a summary of errors encountered during execution"""
    error_stats = error_handler.get_error_stats()

    return {
        "total_errors": sum(error_stats.values()),
        "error_breakdown": error_stats,
        "most_common_errors": sorted(error_stats.items(), key=lambda x: x[1], reverse=True)[:5]
    }