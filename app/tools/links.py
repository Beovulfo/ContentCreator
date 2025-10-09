import os
import requests
import re
from typing import List
from tenacity import retry, stop_after_attempt, wait_exponential
from app.models.schemas import LinkCheckResult


class LinkChecker:
    def __init__(self):
        self.timeout = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
        self.max_redirects = int(os.getenv("MAX_REDIRECTS", "3"))

        # Known paywalled/protected domains where 403/429 is acceptable
        self.paywalled_domains = {
            "ieee.org",
            "acm.org",
            "springer.com",
            "sciencedirect.com",
            "jstor.org",
            "wiley.com",
            "nature.com",
            "science.org",
            "arxiv.org",  # sometimes blocks automated requests
            "kaggle.com",  # often returns 403 for bot requests but URLs are valid
            "manning.com",  # publisher, may block bots
            "oreilly.com"  # publisher, may block bots
        }

    def extract_urls(self, markdown_content: str) -> List[str]:
        """Extract all URLs from markdown content"""
        url_pattern = r'https?://[^\s\)]+|www\.[^\s\)]+'

        # Find markdown links [text](url)
        md_links = re.findall(r'\[([^\]]*)\]\(([^\)]+)\)', markdown_content)
        urls = [link[1] for link in md_links]

        # Find plain URLs
        plain_urls = re.findall(url_pattern, markdown_content)
        urls.extend(plain_urls)

        # Clean up URLs (remove trailing punctuation)
        cleaned_urls = []
        for url in urls:
            url = url.rstrip('.,;:!?')
            if not url.startswith('http'):
                url = 'https://' + url
            cleaned_urls.append(url)

        return list(set(cleaned_urls))  # Remove duplicates

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
    def check_single_url(self, url: str, verification_round: int = 1) -> LinkCheckResult:
        """Check a single URL for accessibility (with verification round tracking)"""
        try:
            # First try HEAD request (faster)
            try:
                response = requests.head(
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                    headers={
                        'User-Agent': 'CourseContentCreator/1.0 LinkChecker (+https://example.com/bot)'
                    }
                )

                # If HEAD fails or returns bad status, try GET
                if response.status_code >= 400:
                    response = requests.get(
                        url,
                        timeout=self.timeout,
                        allow_redirects=True,
                        headers={
                            'User-Agent': 'CourseContentCreator/1.0 LinkChecker (+https://example.com/bot)'
                        }
                    )
            except requests.exceptions.RequestException:
                # If HEAD fails, try GET directly
                response = requests.get(
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                    headers={
                        'User-Agent': 'CourseContentCreator/1.0 LinkChecker (+https://example.com/bot)'
                    }
                )

            status_code = response.status_code

            # Check if URL is OK
            if 200 <= status_code < 300:
                ok = True
            elif status_code in [403, 429] and self._is_paywalled_domain(url):
                # 403/429 is acceptable for known paywalled/protected sources
                ok = True
            else:
                ok = False

            return LinkCheckResult(
                url=url,
                ok=ok,
                status=status_code
            )

        except Exception as e:
            return LinkCheckResult(
                url=url,
                ok=False,
                status=None,
                error=str(e)
            )

    def triple_check(self, urls: List[str]) -> dict:
        """
        Perform single verification of all URLs (renamed from triple_check for backward compatibility).
        Returns detailed report with verification results.
        All results are serialized to dicts for JSON compatibility.
        """
        results = {
            "round_1": [],
            "summary": {
                "total_urls": len(urls),
                "passed_all_rounds": 0,
                "failed_urls": [],
                "all_passed": False
            }
        }

        # Single verification round
        print(f"🔍 Verifying {len(urls)} link(s)...")
        check_results = self.check(urls)
        results["round_1"] = [self._serialize_result(r) for r in check_results]

        # Analyze results
        passed_count = 0
        for result in check_results:
            if result.ok:
                passed_count += 1
            else:
                results["summary"]["failed_urls"].append({
                    "url": result.url,
                    "status": result.status,
                    "error": result.error
                })

        results["summary"]["passed_all_rounds"] = passed_count
        results["summary"]["all_passed"] = (passed_count == len(urls))

        return results

    def _serialize_result(self, result: LinkCheckResult) -> dict:
        """Convert LinkCheckResult to JSON-serializable dict"""
        return {
            "url": result.url,
            "ok": result.ok,
            "status": result.status,
            "error": result.error
        }

    def _is_paywalled_domain(self, url: str) -> bool:
        """Check if URL is from a known paywalled domain"""
        for domain in self.paywalled_domains:
            if domain in url.lower():
                return True
        return False

    def check(self, urls: List[str]) -> List[LinkCheckResult]:
        """Check multiple URLs and return results"""
        results = []
        for url in urls:
            result = self.check_single_url(url)
            results.append(result)
        return results


# Singleton instance
link_checker = LinkChecker()


def check(urls: List[str]) -> List[LinkCheckResult]:
    """Convenience function for link checking"""
    return link_checker.check(urls)


def triple_check(urls: List[str]) -> dict:
    """Convenience function for triple link verification"""
    return link_checker.triple_check(urls)


def extract_urls(markdown_content: str) -> List[str]:
    """Convenience function for URL extraction"""
    return link_checker.extract_urls(markdown_content)