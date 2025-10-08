import os
import re
import requests
from typing import List, Dict, Optional
from tenacity import retry, stop_after_attempt, wait_exponential


class DatasetChecker:
    """Verify that recommended datasets exist and are accessible"""

    def __init__(self):
        self.timeout = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))

        # Kaggle dataset URL pattern (only match /datasets/ URLs)
        self.kaggle_pattern = r'kaggle\.com/datasets/([^/]+)/([^/\s\)]+)'

        # Other dataset repository patterns
        self.dataset_patterns = {
            'uci': r'archive\.ics\.uci\.edu/(?:ml/)?datasets/([^/\s\)]+)',
            'github': r'github\.com/([^/]+)/([^/\s\)]+)',
            'huggingface': r'huggingface\.co/datasets/([^/\s\)]+)',
            'data.gov': r'data\.gov/dataset/([^/\s\)]+)',
        }

    def extract_datasets(self, markdown_content: str) -> List[Dict[str, str]]:
        """Extract all dataset references from markdown content"""
        datasets = []

        # Extract Kaggle datasets (priority)
        matches = re.finditer(self.kaggle_pattern, markdown_content, re.IGNORECASE)
        for match in matches:
            url = f"https://www.kaggle.com/datasets/{match.group(1)}/{match.group(2)}"
            datasets.append({
                'url': url,
                'source': 'kaggle',
                'identifier': f"{match.group(1)}/{match.group(2)}",
                'priority': 'high'
            })

        # Extract other dataset sources
        for source, pattern in self.dataset_patterns.items():
            matches = re.finditer(pattern, markdown_content, re.IGNORECASE)
            for match in matches:
                full_url = match.group(0)
                if not full_url.startswith('http'):
                    full_url = 'https://' + full_url
                datasets.append({
                    'url': full_url,
                    'source': source,
                    'identifier': match.group(1),
                    'priority': 'medium'
                })

        # Remove duplicates
        seen = set()
        unique_datasets = []
        for ds in datasets:
            if ds['url'] not in seen:
                seen.add(ds['url'])
                unique_datasets.append(ds)

        return unique_datasets

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
    def check_kaggle_dataset(self, username: str, dataset_name: str) -> Dict:
        """Check if a Kaggle dataset exists using their API"""
        url = f"https://www.kaggle.com/datasets/{username}/{dataset_name}"

        try:
            # Try HEAD first
            response = requests.head(
                url,
                timeout=self.timeout,
                allow_redirects=True,
                headers={'User-Agent': 'CourseContentCreator/1.0 DatasetChecker'}
            )

            # If HEAD fails, try GET
            if response.status_code >= 400:
                response = requests.get(
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                    headers={'User-Agent': 'CourseContentCreator/1.0 DatasetChecker'}
                )

            # Accept 200-299 or 403 (Kaggle often blocks bots but dataset is valid)
            exists = (200 <= response.status_code < 300) or response.status_code == 403
            return {
                'url': url,
                'exists': exists,
                'status_code': response.status_code,
                'source': 'kaggle',
                'accessible': exists
            }

        except Exception as e:
            return {
                'url': url,
                'exists': False,
                'status_code': None,
                'source': 'kaggle',
                'accessible': False,
                'error': str(e)
            }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
    def check_generic_dataset(self, url: str, source: str) -> Dict:
        """Check if a generic dataset URL is accessible"""
        try:
            # Try HEAD first
            response = requests.head(
                url,
                timeout=self.timeout,
                allow_redirects=True,
                headers={'User-Agent': 'CourseContentCreator/1.0 DatasetChecker'}
            )

            # If HEAD fails, try GET
            if response.status_code >= 400:
                response = requests.get(
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                    headers={'User-Agent': 'CourseContentCreator/1.0 DatasetChecker'}
                )

            # Accept 200-299 or 403 (sites may block bots)
            accessible = (200 <= response.status_code < 300) or response.status_code == 403
            return {
                'url': url,
                'exists': accessible,
                'status_code': response.status_code,
                'source': source,
                'accessible': accessible
            }

        except Exception as e:
            return {
                'url': url,
                'exists': False,
                'status_code': None,
                'source': source,
                'accessible': False,
                'error': str(e)
            }

    def verify_all(self, markdown_content: str) -> Dict:
        """
        Verify all datasets mentioned in content.
        Returns comprehensive report with priority sorting.
        """
        datasets = self.extract_datasets(markdown_content)

        if not datasets:
            return {
                'total_datasets': 0,
                'kaggle_datasets': 0,
                'verified_datasets': [],
                'failed_datasets': [],
                'all_verified': True,
                'has_kaggle': False
            }

        verified = []
        failed = []

        for ds in datasets:
            if ds['source'] == 'kaggle':
                # Parse Kaggle URL
                username, dataset_name = ds['identifier'].split('/')
                result = self.check_kaggle_dataset(username, dataset_name)
            else:
                result = self.check_generic_dataset(ds['url'], ds['source'])

            result['priority'] = ds['priority']

            if result['accessible']:
                verified.append(result)
            else:
                failed.append(result)

        kaggle_count = sum(1 for ds in datasets if ds['source'] == 'kaggle')

        return {
            'total_datasets': len(datasets),
            'kaggle_datasets': kaggle_count,
            'verified_datasets': verified,
            'failed_datasets': failed,
            'all_verified': len(failed) == 0,
            'has_kaggle': kaggle_count > 0
        }


# Singleton instance
dataset_checker = DatasetChecker()


def extract_datasets(markdown_content: str) -> List[Dict[str, str]]:
    """Convenience function for dataset extraction"""
    return dataset_checker.extract_datasets(markdown_content)


def verify_datasets(markdown_content: str) -> Dict:
    """Convenience function for dataset verification"""
    return dataset_checker.verify_all(markdown_content)
