"""Tests for auto.findings — heuristic finding detection."""

from auto.findings import extract_findings_summary, has_findings


class TestHasFindings:
    def test_empty_string_no_findings(self):
        assert has_findings("") is False

    def test_clean_review_returns_false(self):
        text = "Code review complete. No issues found. Approved to ship."
        assert has_findings(text) is False

    def test_explicit_findings_returns_true(self):
        text = "Findings: 3 critical issues identified in auth module."
        assert has_findings(text) is True

    def test_critical_keyword_returns_true(self):
        text = "Several critical issues need to be addressed before merge."
        assert has_findings(text) is True

    def test_security_keyword_returns_true(self):
        text = "Found a security concern in the password handling logic."
        assert has_findings(text) is True

    def test_must_fix_returns_true(self):
        text = "This must fix before we can merge."
        assert has_findings(text) is True

    def test_clean_overrides_finding(self):
        # If both phrases appear, clean wins (review explicitly approved)
        text = "Initial concerns about minor severity items, but no issues found, approved."
        assert has_findings(text) is False

    def test_red_emoji_indicates_findings(self):
        text = "Review summary:\n🔴 Auth bypass possible"
        assert has_findings(text) is True

    def test_check_emoji_alone_is_clean(self):
        text = "✅ All checks passed"
        assert has_findings(text) is False

    def test_normal_prose_no_findings(self):
        text = "Implemented the feature as specified. Tests cover the happy path."
        assert has_findings(text) is False

    def test_blocker_word_returns_true(self):
        text = "Found 1 blocker that prevents merge."
        assert has_findings(text) is True

    def test_not_ready_returns_true(self):
        text = "This implementation is not ready for production."
        assert has_findings(text) is True


class TestExtractFindingsSummary:
    def test_empty_returns_empty(self):
        assert extract_findings_summary("") == ""

    def test_no_match_returns_tail(self):
        text = "Some random output\nwith multiple lines\nand stuff"
        result = extract_findings_summary(text, max_chars=100)
        assert result.endswith("and stuff")

    def test_extracts_around_match(self):
        text = (
            "Setting things up.\n"
            "Running checks.\n"
            "Findings: 2 issues found.\n"
            "Issue 1: missing input validation.\n"
            "Issue 2: hardcoded secret in config.\n"
            "Done.\n"
        )
        result = extract_findings_summary(text)
        assert "Findings:" in result
        assert "Issue 1" in result
        assert "Issue 2" in result

    def test_truncates_long_output(self):
        # Make a long block after the match
        long_block = "\n".join(f"line {i}: details and details" for i in range(200))
        text = f"Findings: ohno\n{long_block}"
        result = extract_findings_summary(text, max_chars=500)
        assert len(result) <= 600  # some margin for truncation marker
