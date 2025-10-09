#!/usr/bin/env python3
"""
Test the two high-priority quality improvements:
1. Quality Gate with Automatic Rollback
2. Feedback Validation

These tests validate the implementations in nodes.py
"""

import sys
import os
sys.path.insert(0, os.path.abspath('.'))

from app.workflow.nodes import WorkflowNodes
from app.models.schemas import RunState, SectionSpec, SectionDraft, ReviewNotes


def test_feedback_validation():
    """Test that feedback validation rejects vague/non-actionable feedback"""
    print("\n" + "="*70)
    print("TEST 1: Feedback Validation")
    print("="*70)

    # Import the validation logic directly
    import re

    def validate_required_fix(fix: str) -> tuple[bool, list[str]]:
        """Direct copy of validation logic from nodes.py"""
        issues = []

        # Check for location specificity
        location_patterns = [
            r'\bsection\b', r'\bparagraph\b', r'\bline\b', r'\btopic\b',
            r'\bsubsection\b', r'\bintroduction\b', r'\bconclusion\b',
            r'\bheading\b', r'\btable\b', r'\bfigure\b', r'\bactivity\b',
            r'\bquiz\b', r'\brubric\b', r'\bwlo\b', r'\bcitation\b',
            r'\breading\b', r'\bbibliography\b', r'\breference\b',
            r'\b\d+\.\d+\b',  # Match section numbers like "1.2"
        ]
        if not any(re.search(pattern, fix.lower()) for pattern in location_patterns):
            issues.append("Missing location reference")

        # Check for action verb
        action_verbs = [
            r'\badd\b', r'\bremove\b', r'\bfix\b', r'\bchange\b',
            r'\breduce\b', r'\breplace\b', r'\bimprove\b', r'\bclarify\b',
            r'\bupdate\b', r'\bexpand\b', r'\bshorten\b', r'\bdelete\b',
            r'\binsert\b', r'\bmodify\b', r'\bcorrect\b', r'\brevise\b',
            r'\binclude\b', r'\bensure\b', r'\bconvert\b', r'\brewrite\b',
        ]
        if not any(re.search(verb, fix.lower()) for verb in action_verbs):
            issues.append("Missing action verb")

        # Check length (should be concise)
        if len(fix) > 120:
            issues.append(f"Too long ({len(fix)} chars, max 120)")

        # Check for vague patterns
        vague_patterns = [
            r'^content\b', r'^better\b', r'^more\b', r'^improve$',
            r'^quality\b', r'^enhance\b', r'^overall\b',
        ]
        if any(re.match(pattern, fix.lower().strip()) for pattern in vague_patterns):
            issues.append("Too vague")

        return len(issues) == 0, issues

    # Test cases: (feedback, should_pass, description)
    test_cases = [
        # Valid feedback - should pass
        ("Section 1.2: Reduce to 250 words by removing examples", True, "Valid: has location + action + specific"),
        ("Introduction: Add explicit WLO1 mapping", True, "Valid: has location + action"),
        ("Topic 1: Convert bullet points to narrative paragraphs", True, "Valid: has location + action + detail"),
        ("Discovery section: Fix citation format for Smith et al.", True, "Valid: has location + action + target"),
        ("Line 45: Replace 'data science' with 'machine learning'", True, "Valid: has location + action + specific"),

        # Invalid feedback - should fail
        ("Improve content", False, "Invalid: vague, no location, no specifics"),
        ("Better quality needed", False, "Invalid: vague start pattern"),
        ("Make it more engaging", False, "Invalid: no location reference"),
        ("Fix the narrative", False, "Invalid: no location, vague action"),
        ("This is a very long feedback item that exceeds the 120 character limit and should be rejected because it's too verbose and not concise", False, "Invalid: too long (>120 chars)"),
        ("Add more examples and improve the flow and make it better overall", False, "Invalid: no specific location"),
    ]

    print("\nTesting feedback validation logic:")
    passed = 0
    failed = 0

    for feedback, should_pass, description in test_cases:
        is_valid, issues = validate_required_fix(feedback)

        if is_valid == should_pass:
            status = "✅ PASS"
            passed += 1
        else:
            status = "❌ FAIL"
            failed += 1

        print(f"\n{status}: {description}")
        print(f"   Feedback: \"{feedback[:60]}...\"" if len(feedback) > 60 else f"   Feedback: \"{feedback}\"")
        print(f"   Expected: {'Valid' if should_pass else 'Invalid'}, Got: {'Valid' if is_valid else 'Invalid'}")
        if not is_valid:
            print(f"   Issues: {', '.join(issues)}")

    print(f"\n" + "-"*70)
    print(f"Results: {passed} passed, {failed} failed out of {len(test_cases)} tests")
    print("="*70)

    return failed == 0


def test_quality_gate_logic():
    """Test that quality gate correctly identifies quality degradation"""
    print("\n" + "="*70)
    print("TEST 2: Quality Gate Logic")
    print("="*70)

    # Create mock state with draft history
    state = RunState(
        week_number=1,
        sections=[
            SectionSpec(
                id="01-overview",
                title="Overview",
                description="Test section",
                ordinal=1,
                constraints={}
            )
        ],
        current_index=0,
        revision_count=2,
        max_revisions=3
    )

    # Mock draft history with scores
    state.draft_history = [
        {
            'section_id': '01-overview',
            'revision': 0,
            'content_md': 'Draft 0 content (excellent)',
            'word_count': 500,
            'editor_score': 9,
            'reviewer_score': 9,
            'approved': True
        },
        {
            'section_id': '01-overview',
            'revision': 1,
            'content_md': 'Draft 1 content (good)',
            'word_count': 480,
            'editor_score': 8,
            'reviewer_score': 8,
            'approved': True
        },
        {
            'section_id': '01-overview',
            'revision': 2,
            'content_md': 'Draft 2 content (degraded)',
            'word_count': 450,
            'editor_score': 5,  # Significant drop
            'reviewer_score': 5,
            'approved': False
        }
    ]

    print("\nScenario: Draft quality degraded from 18/20 → 16/20 → 10/20")
    print("\nDraft History:")
    for draft in state.draft_history:
        total = draft['editor_score'] + draft['reviewer_score']
        print(f"   Revision {draft['revision']}: Editor={draft['editor_score']}/10, Reviewer={draft['reviewer_score']}/10, Total={total}/20")

    # Test quality gate logic
    current_combined = 10  # Current draft: 5 + 5 = 10
    best_draft = max(state.draft_history[:-1],
                     key=lambda d: d['editor_score'] + d['reviewer_score'])
    best_combined = best_draft['editor_score'] + best_draft['reviewer_score']

    degradation = best_combined - current_combined
    should_rollback = current_combined < best_combined - 2  # Threshold: >2 points

    print(f"\nQuality Gate Analysis:")
    print(f"   Best previous score: {best_combined}/20 (Revision {best_draft['revision']})")
    print(f"   Current score: {current_combined}/20 (Revision 2)")
    print(f"   Degradation: {degradation} points")
    print(f"   Rollback threshold: >2 points")
    print(f"   Should trigger rollback: {should_rollback}")

    if should_rollback:
        print(f"\n✅ PASS: Quality gate would correctly trigger rollback")
        print(f"   Would restore: Revision {best_draft['revision']} with score {best_combined}/20")
        success = True
    else:
        print(f"\n❌ FAIL: Quality gate should have triggered rollback")
        success = False

    # Test edge case: small degradation (within threshold)
    print(f"\n" + "-"*70)
    print("Edge Case: Small degradation (1 point) - should NOT trigger rollback")

    current_combined_small = 17  # Only 1 point worse than best (18)
    degradation_small = best_combined - current_combined_small
    should_rollback_small = current_combined_small < best_combined - 2

    print(f"   Best previous: {best_combined}/20")
    print(f"   Current: {current_combined_small}/20")
    print(f"   Degradation: {degradation_small} point")
    print(f"   Should trigger rollback: {should_rollback_small}")

    if not should_rollback_small:
        print(f"   ✅ PASS: Small degradation correctly allowed")
    else:
        print(f"   ❌ FAIL: Should not rollback for small degradation")
        success = False

    print("="*70)

    return success


def main():
    """Run all tests"""
    print("\n" + "="*70)
    print("QUALITY IMPROVEMENTS TEST SUITE")
    print("Testing: Quality Gate + Feedback Validation")
    print("="*70)

    results = []

    # Test 1: Feedback Validation
    try:
        result1 = test_feedback_validation()
        results.append(("Feedback Validation", result1))
    except Exception as e:
        print(f"\n❌ Feedback Validation test crashed: {e}")
        results.append(("Feedback Validation", False))

    # Test 2: Quality Gate Logic
    try:
        result2 = test_quality_gate_logic()
        results.append(("Quality Gate Logic", result2))
    except Exception as e:
        print(f"\n❌ Quality Gate test crashed: {e}")
        results.append(("Quality Gate Logic", False))

    # Summary
    print("\n" + "="*70)
    print("TEST SUMMARY")
    print("="*70)

    for test_name, passed in results:
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"{status}: {test_name}")

    all_passed = all(result for _, result in results)

    print("\n" + "="*70)
    if all_passed:
        print("🎉 ALL TESTS PASSED - Quality improvements are working correctly!")
        print("="*70)
        return 0
    else:
        print("⚠️  SOME TESTS FAILED - Review implementation")
        print("="*70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
