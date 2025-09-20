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

        # Known paywalled domains where 403 is acceptable
        self.paywalled_domains = {
            "ieee.org",
            "acm.org",
            "springer.com",
            "sciencedirect.com",
            "jstor.org",
            "wiley.com",
            "nature.com",
            "science.org",
            "arxiv.org"  # sometimes blocks automated requests
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
    def check_single_url(self, url: str) -> LinkCheckResult:
        """Check a single URL for accessibility"""
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
            elif status_code == 403 and self._is_paywalled_domain(url):
                # 403 is acceptable for known paywalled scholarly sources
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


def extract_urls(markdown_content: str) -> List[str]:
    """Convenience function for URL extraction"""
    return link_checker.extract_urls(markdown_content)