import os
import requests
import json
from typing import List, Dict, Optional, Any
from tenacity import retry, stop_after_attempt, wait_exponential
from app.models.schemas import WebSearchResult


class WebSearchTool:
    def __init__(self):
        # Rate limiting and cache
        self.last_request_time = {}
        self.min_request_interval = 1.0  # seconds between requests
        self.request_cache = {}
        self.cache_ttl = 300  # 5 minutes cache TTL

    @property
    def tavily_key(self):
        return os.getenv("TAVILY_API_KEY")

    @property
    def bing_key(self):
        return os.getenv("BING_SEARCH_API_KEY")

    @property
    def serp_key(self):
        return os.getenv("SERPAPI_API_KEY")

    @property
    def google_cse_key(self):
        return os.getenv("GOOGLE_CSE_KEY")

    @property
    def google_cse_id(self):
        return os.getenv("GOOGLE_CSE_ID")

    @property
    def timeout(self):
        return int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))

    @property
    def provider_priority(self):
        """Lazy evaluation of provider priority"""
        return self._determine_provider_priority()

    def search(self, query: str, top_k: int = 5, recency_days: Optional[int] = 730) -> List[WebSearchResult]:
        """
        Unified web search interface with provider failover and caching.
        Tries providers in priority order until one succeeds.
        """
        # Check cache first
        cache_key = f"{query}:{top_k}:{recency_days}"
        cached_result = self._get_cached_result(cache_key)
        if cached_result:
            return cached_result

        # Try each provider in priority order
        last_error = None
        for provider in self.provider_priority:
            try:
                # Rate limiting
                self._respect_rate_limits(provider)

                # Attempt search with current provider
                results = self._search_with_provider(provider, query, top_k)

                if results:
                    # Cache successful result
                    self._cache_result(cache_key, results)
                    return results

            except Exception as e:
                last_error = e
                print(f"⚠️  {provider} search failed: {str(e)}")
                continue

        # If all providers failed
        if last_error:
            print(f"💥 All search providers failed. Last error: {str(last_error)}")
            return []  # Return empty list instead of raising exception

        # No providers configured
        print("⚠️  No search providers configured")
        return []

    def _determine_provider_priority(self) -> List[str]:
        """Determine which providers are available and their priority order"""
        priority_order = ["tavily", "bing", "serpapi", "google_cse"]
        available_providers = []

        for provider in priority_order:
            if self._is_provider_available(provider):
                available_providers.append(provider)

        return available_providers

    def _is_provider_available(self, provider: str) -> bool:
        """Check if a provider is configured"""
        if provider == "tavily":
            return bool(self.tavily_key)
        elif provider == "bing":
            return bool(self.bing_key)
        elif provider == "serpapi":
            return bool(self.serp_key)
        elif provider == "google_cse":
            return bool(self.google_cse_key and self.google_cse_id)
        return False

    def _search_with_provider(self, provider: str, query: str, top_k: int) -> List[WebSearchResult]:
        """Search using a specific provider"""
        if provider == "tavily":
            return self._search_tavily(query, top_k)
        elif provider == "bing":
            return self._search_bing(query, top_k)
        elif provider == "serpapi":
            return self._search_serpapi(query, top_k)
        elif provider == "google_cse":
            return self._search_google_cse(query, top_k)
        else:
            raise ValueError(f"Unknown provider: {provider}")

    def _respect_rate_limits(self, provider: str):
        """Implement rate limiting for providers"""
        import time

        current_time = time.time()
        last_time = self.last_request_time.get(provider, 0)

        time_since_last = current_time - last_time
        if time_since_last < self.min_request_interval:
            sleep_time = self.min_request_interval - time_since_last
            time.sleep(sleep_time)

        self.last_request_time[provider] = time.time()

    def _get_cached_result(self, cache_key: str) -> Optional[List[WebSearchResult]]:
        """Check if result is in cache and still valid"""
        import time

        if cache_key in self.request_cache:
            cached_time, cached_result = self.request_cache[cache_key]
            if time.time() - cached_time < self.cache_ttl:
                return cached_result

        return None

    def _cache_result(self, cache_key: str, results: List[WebSearchResult]):
        """Cache search results"""
        import time
        self.request_cache[cache_key] = (time.time(), results)

        # Clean old cache entries periodically
        if len(self.request_cache) > 100:  # Arbitrary limit
            self._cleanup_cache()

    def _cleanup_cache(self):
        """Remove expired cache entries"""
        import time

        current_time = time.time()
        expired_keys = []

        for key, (cached_time, _) in self.request_cache.items():
            if current_time - cached_time > self.cache_ttl:
                expired_keys.append(key)

        for key in expired_keys:
            del self.request_cache[key]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        retry_error_callback=lambda retry_state: print(f"Retrying Tavily search: attempt {retry_state.attempt_number}")
    )
    def _search_tavily(self, query: str, top_k: int) -> List[WebSearchResult]:
        """Search using Tavily API"""
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": self.tavily_key,
            "query": query,
            "max_results": top_k,
            "include_answer": False,
            "search_depth": "advanced"
        }

        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()

            results = []
            for result in data.get("results", []):
                results.append(WebSearchResult(
                    title=result.get("title", ""),
                    url=result.get("url", ""),
                    snippet=result.get("content", ""),
                    published=result.get("published_time")
                ))
            return results

        except requests.exceptions.Timeout:
            raise Exception("Tavily API timeout")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                raise Exception("Tavily API rate limit exceeded")
            elif e.response.status_code == 403:
                raise Exception("Tavily API authentication failed")
            else:
                raise Exception(f"Tavily API HTTP error: {e.response.status_code}")
        except Exception as e:
            raise Exception(f"Tavily API error: {str(e)}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        retry_error_callback=lambda retry_state: print(f"Retrying Bing search: attempt {retry_state.attempt_number}")
    )
    def _search_bing(self, query: str, top_k: int) -> List[WebSearchResult]:
        """Search using Bing Search API"""
        url = "https://api.bing.microsoft.com/v7.0/search"
        headers = {"Ocp-Apim-Subscription-Key": self.bing_key}
        params = {
            "q": query,
            "count": top_k,
            "responseFilter": "Webpages",
            "textDecorations": False,
            "textFormat": "Raw"
        }

        try:
            response = requests.get(url, headers=headers, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("webPages", {}).get("value", []):
                results.append(WebSearchResult(
                    title=item.get("name", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", ""),
                    published=item.get("dateLastCrawled")
                ))
            return results

        except requests.exceptions.Timeout:
            raise Exception("Bing API timeout")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                raise Exception("Bing API rate limit exceeded")
            elif e.response.status_code == 401:
                raise Exception("Bing API authentication failed")
            else:
                raise Exception(f"Bing API HTTP error: {e.response.status_code}")
        except Exception as e:
            raise Exception(f"Bing API error: {str(e)}")

    def _search_serpapi(self, query: str, top_k: int) -> List[WebSearchResult]:
        """Search using SerpAPI"""
        url = "https://serpapi.com/search"
        params = {
            "engine": "google",
            "q": query,
            "api_key": self.serp_key,
            "num": top_k,
            "format": "json"
        }

        response = requests.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()

        results = []
        for item in data.get("organic_results", []):
            results.append(WebSearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                published=item.get("date")
            ))
        return results

    def _search_google_cse(self, query: str, top_k: int) -> List[WebSearchResult]:
        """Search using Google Custom Search Engine"""
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": self.google_cse_key,
            "cx": self.google_cse_id,
            "q": query,
            "num": min(top_k, 10)  # Google CSE max is 10
        }

        response = requests.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()

        results = []
        for item in data.get("items", []):
            results.append(WebSearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                published=item.get("pagemap", {}).get("metatags", [{}])[0].get("date")
            ))
        return results

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def fetch(self, url: str) -> Dict:
        """
        Fetch content from a specific URL.
        Returns basic metadata and content snippet.
        """
        try:
            response = requests.get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
                headers={
                    'User-Agent': 'CourseContentCreator/1.0 (+https://example.com/bot)'
                }
            )
            response.raise_for_status()

            # Extract basic info - in a real implementation you might want to parse HTML
            content_length = len(response.text)
            content_preview = response.text[:1000] if response.text else ""

            return {
                "url": url,
                "status": response.status_code,
                "content_length": content_length,
                "content_preview": content_preview,
                "headers": dict(response.headers),
                "final_url": response.url
            }
        except Exception as e:
            return {
                "url": url,
                "status": None,
                "error": str(e),
                "content_length": 0,
                "content_preview": "",
                "headers": {},
                "final_url": url
            }

    def check_provider_health(self) -> Dict[str, Dict[str, Any]]:
        """Check health of all configured providers"""
        health_status = {}

        for provider in self.provider_priority:
            health_status[provider] = {"available": True, "status": "unknown", "last_error": None}

            try:
                # Perform a minimal test search
                test_results = self._search_with_provider(provider, "test", 1)

                if test_results:
                    health_status[provider].update({
                        "status": "healthy",
                        "test_results_count": len(test_results)
                    })
                else:
                    health_status[provider]["status"] = "no_results"

            except Exception as e:
                health_status[provider].update({
                    "available": False,
                    "status": "error",
                    "last_error": str(e)
                })

        return health_status

    def get_provider_info(self) -> Dict[str, Any]:
        """Get information about configured providers and their status"""
        return {
            "available_providers": self.provider_priority,
            "provider_count": len(self.provider_priority),
            "cache_entries": len(self.request_cache),
            "cache_ttl_seconds": self.cache_ttl,
            "rate_limit_interval": self.min_request_interval
        }


# Singleton instance - lazy initialization
_web_tool_instance = None

def get_web_tool() -> WebSearchTool:
    """Get the singleton WebSearchTool instance with lazy initialization"""
    global _web_tool_instance
    if _web_tool_instance is None:
        _web_tool_instance = WebSearchTool()
    return _web_tool_instance

# Create a lazy property-like object for backward compatibility
class _WebToolProxy:
    def __getattr__(self, name):
        return getattr(get_web_tool(), name)

    def __call__(self, *args, **kwargs):
        return get_web_tool()(*args, **kwargs)

web_tool = _WebToolProxy()


def search(query: str, top_k: int = 5, recency_days: Optional[int] = 730) -> List[WebSearchResult]:
    """Convenience function for web search"""
    return web_tool.search(query, top_k, recency_days)


def fetch(url: str) -> Dict:
    """Convenience function for URL fetching"""
    return web_tool.fetch(url)