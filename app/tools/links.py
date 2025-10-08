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
        Perform triple verification of all URLs.
        Returns detailed report with all three verification rounds.
        All results are serialized to dicts for JSON compatibility.
        """
        results = {
            "round_1": [],
            "round_2": [],
            "round_3": [],
            "summary": {
                "total_urls": len(urls),
                "passed_all_rounds": 0,
                "failed_urls": [],
                "all_passed": False
            }
        }

        # Round 1
        print(f"🔍 Link Verification Round 1/3...")
        round_1_results = self.check(urls)
        results["round_1"] = [self._serialize_result(r) for r in round_1_results]

        # Round 2
        print(f"🔍 Link Verification Round 2/3...")
        round_2_results = self.check(urls)
        results["round_2"] = [self._serialize_result(r) for r in round_2_results]

        # Round 3
        print(f"🔍 Link Verification Round 3/3...")
        round_3_results = self.check(urls)
        results["round_3"] = [self._serialize_result(r) for r in round_3_results]

        # Analyze results - URL must pass ALL three rounds
        url_pass_count = {}
        for url in urls:
            url_pass_count[url] = 0

        for round_results in [round_1_results, round_2_results, round_3_results]:
            for result in round_results:
                if result.ok:
                    url_pass_count[result.url] += 1

        # Count URLs that passed all three rounds
        for url, passes in url_pass_count.items():
            if passes == 3:
                results["summary"]["passed_all_rounds"] += 1
            else:
                results["summary"]["failed_urls"].append({
                    "url": url,
                    "passed_rounds": passes,
                    "failed_rounds": 3 - passes
                })

        results["summary"]["all_passed"] = (
            results["summary"]["passed_all_rounds"] == results["summary"]["total_urls"]
        )

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