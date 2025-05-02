import requests
import hashlib
import time
import random
import asyncio
import logging
import os
import base64
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from django.utils import timezone
from django.conf import settings
from django.db import transaction
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright
from .models import Proxy, CrawlJob, CrawledURL, CrawlStats

logger = logging.getLogger(__name__)

# Ensure the screenshots directory exists
SCREENSHOTS_DIR = os.path.join(settings.BASE_DIR, 'static', 'screenshots')
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

class WebshareProxyService:
    """Service to interact with WebShare API for proxy management"""
    
    API_BASE_URL = "https://proxy.webshare.io/api/v2/proxy/list/"
    API_KEY = ""  # This should be in environment variables
    
    @classmethod
    def get_headers(cls):
        return {
            "Authorization": f"Token {cls.API_KEY}"
        }
    
    @classmethod
    def fetch_proxies(cls, page=1, page_size=25, countries=None):
        """Fetch proxies from WebShare API with optional country filtering"""
        url = f"{cls.API_BASE_URL}?mode=direct&page={page}&page_size={page_size}"
        
        # Add country filtering if provided
        if countries:
            # For WebShare API, country filtering is done like country_code=US,GB,CA
            if isinstance(countries, list):
                countries_str = ','.join(countries)
            else:
                countries_str = countries
            url += f"&country_code={countries_str}"
            
        try:
            response = requests.get(url, headers=cls.get_headers())
            response.raise_for_status()
            data = response.json()
            return data.get('results', [])
        except requests.RequestException as e:
            logger.error(f"Error fetching proxies: {str(e)}")
            return []
    
    @classmethod
    def get_available_countries(cls):
        """Get list of available countries from current proxies"""
        countries = Proxy.objects.filter(
            is_blocked=False
        ).values_list('country_code', flat=True).distinct()
        
        # Return only non-empty country codes
        return [c for c in countries if c]
    
    @classmethod
    def sync_proxies(cls):
        """Sync proxies from WebShare API to local database"""
        proxies = cls.fetch_proxies(page_size=100)
        
        for proxy_data in proxies:
            # Check if proxy already exists in the database
            proxy, created = Proxy.objects.update_or_create(
                ip_address=proxy_data.get('proxy_address'),
                port=proxy_data.get('port'),
                defaults={
                    'username': proxy_data.get('username'),
                    'password': proxy_data.get('password'),
                    'country_code': proxy_data.get('country_code'),
                }
            )
            
        return len(proxies)
    
    @classmethod
    def get_available_proxy(cls, countries=None, reshuffle=False, used_proxies=None):
        """
        Get an available proxy using different selection strategies
        
        Args:
            countries: Optional list or comma-separated string of country codes to filter by
            reshuffle: If True, use round-robin selection instead of least recently used
            used_proxies: List of proxy IDs already used in this session (for round-robin)
        
        Returns:
            A Proxy object or None if no proxies are available
        """
        # Create base query for unblocked proxies
        query = Proxy.objects.filter(is_blocked=False)
        
        # Add country filtering if provided
        if countries:
            if isinstance(countries, str):
                # Split comma-separated string into list
                countries = [c.strip() for c in countries.split(',') if c.strip()]
            
            if countries:  # Only filter if we have valid countries
                query = query.filter(country_code__in=countries)
        
        # No available proxies? Try unblocking some that have been in cooloff long enough
        if not query.exists():
            cooloff_time = timezone.now() - timedelta(minutes=5)
            blocked_query = Proxy.objects.filter(is_blocked=True, blocked_at__lt=cooloff_time)
            
            # Apply country filtering to blocked proxies too
            if countries and isinstance(countries, list) and countries:
                blocked_query = blocked_query.filter(country_code__in=countries)
                
            blocked_proxies = blocked_query.all()
            
            for proxy in blocked_proxies:
                proxy.is_blocked = False
                proxy.blocked_at = None
                proxy.save(update_fields=['is_blocked', 'blocked_at'])
            
            # Refresh the query with newly unblocked proxies
            query = Proxy.objects.filter(is_blocked=False)
            if countries and isinstance(countries, list) and countries:
                query = query.filter(country_code__in=countries)
        
        # Apply different selection strategies based on reshuffle flag
        if not query.exists():
            return None
            
        if reshuffle:
            # Round-robin: exclude recently used proxies if possible
            if used_proxies and len(used_proxies) < query.count():
                available_proxies = query.exclude(id__in=used_proxies)
                # If we've exhausted all proxies, reset and use all again
                if not available_proxies.exists():
                    available_proxies = query
            else:
                available_proxies = query
                
            # For round-robin, order randomly rather than by last_used
            proxy = available_proxies.order_by('?').first()
        else:
            # Default: Use least-recently used proxy
            proxy = query.order_by('last_used').first()
        
        # Update last_used timestamp
        if proxy:
            proxy.last_used = timezone.now()
            proxy.save(update_fields=['last_used'])
            
        return proxy

class CrawlerService:
    """Service for crawling URLs with proxy rotation"""
    
    def __init__(self, job_id, debug_mode=False):
        self.job_id = job_id
        self.job = None
        self.stats = None
        self.current_proxy = None
        self.current_rate = 1.0  # Start conservatively with 1 request per second
        self.debug_mode = debug_mode
        self.current_url_id = None  # ID of the URL currently being processed
        self.page_screenshot = None  # Base64-encoded screenshot
        self.used_proxies = []  # Track proxies used for round-robin rotation
    
    @sync_to_async
    def _init_job_and_stats(self):
        """Initialize job and stats objects"""
        self.job = CrawlJob.objects.get(id=self.job_id)
        self.stats, created = CrawlStats.objects.get_or_create(job=self.job)
        logger.info(f"Initialized job {self.job_id} and stats (created: {created})")
        
    def calculate_content_hash(self, content):
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    @sync_to_async
    def _get_available_proxy(self):
        """Get an available proxy in async context, using country filtering if specified"""
        # Get proxy countries from job if set
        countries = self.job.proxy_countries if self.job and self.job.proxy_countries else None
        reshuffle = self.job.reshuffle_proxies if self.job else False
        
        # Get a proxy using the appropriate strategy
        proxy = WebshareProxyService.get_available_proxy(
            countries=countries,
            reshuffle=reshuffle,
            used_proxies=self.used_proxies if reshuffle else None
        )
        
        # Track this proxy for round-robin if we're reshuffling
        if proxy and reshuffle and proxy.id not in self.used_proxies:
            self.used_proxies.append(proxy.id)
            
        return proxy
    
    @sync_to_async
    def _update_job_status(self, status, cooloff_until=None):
        """Update job status in async context"""
        self.job.status = status
        if cooloff_until:
            self.job.cooloff_until = cooloff_until
        self.job.save(update_fields=['status', 'cooloff_until'] if cooloff_until else ['status'])
    
    @sync_to_async
    def _update_job_progress(self, success):
        """Update job progress in async context"""
        if success:
            self.job.urls_processed += 1
            self.job.save(update_fields=['urls_processed'])
            
    @sync_to_async
    def _update_rate(self, new_rate):
        """Update crawl rate in async context"""
        self.current_rate = new_rate
        self.job.current_rate = new_rate
        self.job.save(update_fields=['current_rate'])
    
    @sync_to_async
    def _update_proxy_stats(self, proxy=None):
        """Update proxy stats in async context"""
        self.stats.current_proxy = proxy
        self.stats.save(update_fields=['current_proxy'])
    
    @sync_to_async
    def _mark_proxy_blocked(self):
        """Mark current proxy as blocked in async context"""
        if self.current_proxy:
            self.current_proxy.is_blocked = True
            self.current_proxy.blocked_at = timezone.now()
            self.current_proxy.save(update_fields=['is_blocked', 'blocked_at'])
            
            self.job.rate_limit_hits += 1
            self.stats.blocked_proxies_count += 1
            self.job.save(update_fields=['rate_limit_hits'])
            self.stats.save(update_fields=['blocked_proxies_count'])
    
    @sync_to_async
    def _update_url_pre_crawl(self, crawled_url):
        """Update URL before crawling in async context"""
        self.current_url_id = crawled_url.id
        crawled_url.proxy_used = self.current_proxy
        crawled_url.save(update_fields=['proxy_used'])
        return crawled_url
    
    @sync_to_async
    def _update_url_post_crawl(self, crawled_url, content, content_hash, status_code, structured_content=None):
        """Update URL after successful crawl in async context"""
        crawled_url.content = content
        crawled_url.content_hash = content_hash
        crawled_url.status_code = status_code
        crawled_url.crawled_at = timezone.now()
        crawled_url.retry_count = 0
        crawled_url.retry_status = 'success'
        
        # Save structured content if available
        if structured_content:
            crawled_url.structured_content = json.dumps(structured_content, ensure_ascii=False)
            
        crawled_url.save()
        
        # Clear current URL ID after successful crawl
        self.current_url_id = None
        self.page_screenshot = None
        
        # Update stats
        self.stats.successful_requests += 1
        self.stats.last_request_time = timezone.now()
        self.stats.save(update_fields=['successful_requests', 'last_request_time'])
    
    @sync_to_async
    def _update_url_retry(self, crawled_url, is_blocking=False, is_timeout=False, screenshot_path=None):
        """Update URL retry count in async context"""
        crawled_url.retry_count += 1
        
        # If it's a timeout, mark for retry
        if is_timeout:
            crawled_url.retry_status = 'timeout'
        elif crawled_url.retry_status == 'retry_pending' and crawled_url.retry_count >= 2:
            # If this was already a retry attempt and it failed again, mark as failed
            crawled_url.retry_status = 'failed'
        
        # If we have a screenshot, save its path
        if screenshot_path:
            crawled_url.screenshot_path = screenshot_path
        
        crawled_url.save(update_fields=['retry_count', 'retry_status', 'screenshot_path'] 
                         if screenshot_path else ['retry_count', 'retry_status'])
        
        if is_blocking and crawled_url.retry_count >= 3:
            # After 3 retries with same content, assume we're blocked
            self._mark_proxy_blocked()
            
            # Adjust crawl rate more conservatively
            self.current_rate = max(0.2, self.current_rate * 0.5)
            self.job.current_rate = self.current_rate
            self.job.save(update_fields=['current_rate'])
        
        # Update stats for failed request
        self.stats.failed_requests += 1
        self.stats.save(update_fields=['failed_requests'])
    
    @sync_to_async
    def _get_pending_urls(self):
        """Get pending URLs for processing in async context"""
        return list(self.job.urls.filter(
            content__isnull=True, 
            retry_status__in=['pending', 'retry_pending']
        ).order_by('id'))
    
    @sync_to_async
    def _get_timeout_urls(self):
        """Get URLs that timed out and need retry"""
        return list(self.job.urls.filter(retry_status='timeout').order_by('id'))
    
    @sync_to_async
    def _mark_url_for_retry(self, crawled_url):
        """Mark a URL as ready for retry"""
        crawled_url.retry_status = 'retry_pending'
        crawled_url.save(update_fields=['retry_status'])
        return crawled_url
    
    @sync_to_async
    def _check_if_killed(self):
        """Check if the job has been marked as killed"""
        # Refresh the job from the database to get the latest status
        job = CrawlJob.objects.get(id=self.job_id)
        return job.status == 'killed'
    
    @sync_to_async
    def _save_screenshot(self, url_id, screenshot_data):
        """Save screenshot data for a URL"""
        self.page_screenshot = screenshot_data
        
        # Also save to disk
        timestamp = int(time.time())
        filename = f"screenshot_{url_id}_{timestamp}.png"
        filepath = os.path.join(SCREENSHOTS_DIR, filename)
        
        # Decode base64 and save to file
        with open(filepath, 'wb') as f:
            f.write(base64.b64decode(screenshot_data.split(',')[1]))
        
        # Return the relative path to be saved in the database
        return f"screenshots/{filename}"
    
    async def setup_browser(self):
        """Set up a Playwright browser with proxy"""
        playwright = await async_playwright().start()
        
        if not self.current_proxy:
            self.current_proxy = await self._get_available_proxy()
            if not self.current_proxy:
                await self._update_job_status('cooloff', timezone.now() + timedelta(minutes=5))
                return None, None
            
            await self._update_proxy_stats(self.current_proxy)
        
        # Configure browser with proxy
        proxy_config = {
            "server": f"http://{self.current_proxy.ip_address}:{self.current_proxy.port}",
            "username": self.current_proxy.username,
            "password": self.current_proxy.password
        }
        
        # Launch browser with headless mode based on debug setting
        browser_options = {
            "headless": not self.debug_mode,  # Run non-headless in debug mode
            "proxy": proxy_config
        }
        
        browser = await playwright.chromium.launch(**browser_options)
        
        # Create context with viewport settings
        context_options = {"viewport": {"width": 1280, "height": 800}}
        context = await browser.new_context(**context_options)
        
        return playwright, browser
    
    async def crawl_url(self, crawled_url, is_retry=False):
        """Crawl a single URL with the current proxy"""
        playwright, browser = await self.setup_browser()
        
        if not browser:
            return False
        
        try:
            # Update the URL status
            crawled_url = await self._update_url_pre_crawl(crawled_url)
            
            page = await browser.new_page()
            
            # Add debug delay if in debug mode (artificial delay for better visibility)
            if self.debug_mode:
                await asyncio.sleep(1)  # 1 second delay before navigation in debug mode
                
            start_time = time.time()
            
            # Navigate to the URL
            # Use longer timeout for retry attempts
            timeout = 120000 if is_retry else 30000  # 2 minutes for retry, 30 seconds otherwise
            
            try:
                logger.info(f"Starting navigation to {crawled_url.url}")
                
                # In debug mode, wait longer between actions
                if self.debug_mode:
                    # Don't reduce the timeout in debug mode - give it the full time
                    # Take an early screenshot before navigation
                    if page:
                        try:
                            screenshot_data = await page.screenshot(type='png', full_page=True)
                            await self._save_screenshot(
                                crawled_url.id, 
                                f"data:image/png;base64,{base64.b64encode(screenshot_data).decode('utf-8')}"
                            )
                            logger.info("Took pre-navigation screenshot")
                            await asyncio.sleep(2)  # Give time to see the screenshot
                        except Exception as e:
                            logger.error(f"Error taking pre-navigation screenshot: {str(e)}")
                
                # Modified navigation to be more robust
                response = await page.goto(
                    crawled_url.url, 
                    wait_until='domcontentloaded',  # Changed from networkidle to load faster
                    timeout=timeout
                )
                
                logger.info(f"Initial navigation completed with status: {response.status if response else 'None'}")
                
                # After basic navigation, wait for network to become idle with a separate timeout
                if self.debug_mode:
                    try:
                        # Give more time for visual inspection
                        await asyncio.sleep(2)
                        logger.info("Waiting for network idle...")
                        # Take a screenshot after initial load but before network idle
                        screenshot_data = await page.screenshot(type='png', full_page=True)
                        await self._save_screenshot(
                            crawled_url.id, 
                            f"data:image/png;base64,{base64.b64encode(screenshot_data).decode('utf-8')}"
                        )
                        # Wait for network idle separately with longer timeout
                        await page.wait_for_load_state('networkidle', timeout=timeout)
                        logger.info("Network idle reached")
                        await asyncio.sleep(3)  # Extra time to see the final state
                    except Exception as e:
                        logger.warning(f"Network idle timeout: {str(e)}")
                else:
                    try:
                        # For non-debug mode, still wait for network idle but with shorter timeout
                        await page.wait_for_load_state('networkidle', timeout=15000)
                    except Exception as e:
                        logger.warning(f"Network idle timeout in regular mode: {str(e)}")
                
                # Take a final screenshot regardless of network idle status
                if page:
                    screenshot_data = await page.screenshot(type='png', full_page=True)
                    screenshot_path = await self._save_screenshot(
                        crawled_url.id, 
                        f"data:image/png;base64,{base64.b64encode(screenshot_data).decode('utf-8')}"
                    )
                
                # Calculate response time
                response_time = time.time() - start_time
                
                if response:
                    status_code = response.status
                    content = await page.content()
                    
                    # Extract structured content for easier processing
                    structured_content = await self._extract_content(page)
                    
                    # Check if content is useful (not a 404 or blocked page)
                    # If previous content hash exists, compare with new hash
                    content_hash = self.calculate_content_hash(content)
                    
                    if crawled_url.content_hash and crawled_url.content_hash == content_hash:
                        # Same content as before, might be a block page
                        await self._update_url_retry(crawled_url, is_blocking=True, screenshot_path=screenshot_path)
                        return False
                    
                    # Save the successful response with structured content
                    await self._update_url_post_crawl(
                        crawled_url, 
                        content, 
                        content_hash, 
                        status_code,
                        structured_content=structured_content
                    )
                    
                    # Update average response time
                    if self.stats.avg_response_time == 0:
                        self.stats.avg_response_time = response_time
                    else:
                        self.stats.avg_response_time = (self.stats.avg_response_time + response_time) / 2
                    await sync_to_async(self.stats.save)(update_fields=['avg_response_time'])
                    
                    # If successful, slightly increase rate if we've had 10+ successful requests
                    if self.stats.successful_requests % 10 == 0:
                        new_rate = min(5.0, self.current_rate * 1.1)  # Cap at 5 req/sec
                        await self._update_rate(new_rate)
                    
                    return True
                else:
                    # Failed to get a response
                    await self._update_url_retry(crawled_url, screenshot_path=screenshot_path)
                    return False
            except Exception as e:
                # Handle timeouts and other errors
                logger.error(f"Error navigating to {crawled_url.url}: {str(e)}")
                
                # Take a screenshot before handling the error
                screenshot_path = None
                if page:
                    try:
                        screenshot_data = await page.screenshot(type='png', full_page=True)
                        screenshot_path = await self._save_screenshot(
                            crawled_url.id, 
                            f"data:image/png;base64,{base64.b64encode(screenshot_data).decode('utf-8')}"
                        )
                        # In debug mode, add extra delay to see the error state
                        if self.debug_mode:
                            await asyncio.sleep(3)
                    except Exception as screenshot_error:
                        logger.error(f"Error taking error screenshot: {str(screenshot_error)}")
                
                # Explicitly check for timeout
                if "timeout" in str(e).lower():
                    logger.warning(f"Timeout for URL {crawled_url.url}: {str(e)}")
                    await self._update_url_retry(crawled_url, is_timeout=True, screenshot_path=screenshot_path)
                    return False
                else:
                    # Re-raise for general error handling
                    raise
                
        except Exception as e:
            # Handle timeouts and other errors
            logger.error(f"Error crawling {crawled_url.url}: {str(e)}")
            
            # Try to take a screenshot if possible
            screenshot_path = None
            try:
                if page:
                    screenshot_data = await page.screenshot(type='png', full_page=True)
                    screenshot_path = await self._save_screenshot(
                        crawled_url.id, 
                        f"data:image/png;base64,{base64.b64encode(screenshot_data).decode('utf-8')}"
                    )
                    # In debug mode, wait a bit to show the error state
                    if self.debug_mode:
                        await asyncio.sleep(2)
            except:
                logger.error("Failed to take error screenshot")
            
            # Check if this was a timeout
            is_timeout = "timeout" in str(e).lower()
            await self._update_url_retry(crawled_url, is_timeout=is_timeout, screenshot_path=screenshot_path)
            
            # Check if this might be a rate limit or blocking issue
            if "timeout" in str(e).lower() or "navigation failed" in str(e).lower():
                # Potential rate limit - slow down
                new_rate = max(0.2, self.current_rate * 0.8)
                await self._update_rate(new_rate)
                
                # If multiple consecutive failures, mark proxy as blocked
                if crawled_url.retry_count >= 3:
                    await self._mark_proxy_blocked()
                    self.current_proxy = None
                    await self._update_proxy_stats(None)
            
            return False
        finally:
            # In debug mode, add a final delay before closing to allow seeing the final state
            if self.debug_mode:
                await asyncio.sleep(5)  # 5 second delay before closing in debug mode
                
            # Close browser and playwright
            if browser:
                await browser.close()
            if playwright:
                await playwright.stop()
    
    async def process_job(self):
        """Process all URLs in the job"""
        # Initialize job and stats
        await self._init_job_and_stats()
        
        # Update job status
        await self._update_job_status('running')
        
        # Get all pending URLs
        urls = await self._get_pending_urls()
        
        # Process URLs - first pass
        for url in urls:
            # Check if job has been killed
            if await self._check_if_killed():
                logger.info(f"Job {self.job_id} was killed. Stopping.")
                break
                
            if self.job.status == 'cooloff':
                # Wait for cooloff period to complete
                if self.job.cooloff_until and self.job.cooloff_until > timezone.now():
                    wait_time = (self.job.cooloff_until - timezone.now()).total_seconds()
                    await asyncio.sleep(wait_time)
                
                # Check if job was killed during cooloff
                if await self._check_if_killed():
                    logger.info(f"Job {self.job_id} was killed during cooloff. Stopping.")
                    break
                
                # Reset cooloff status
                await self._update_job_status('running')
            
            # Process the URL
            success = await self.crawl_url(url)
            
            # Update job progress
            await self._update_job_progress(success)
            
            # Respect the current rate limit
            await asyncio.sleep(1 / self.current_rate)
        
        # Get URLs that timed out during the first pass
        timeout_urls = await self._get_timeout_urls()
        
        if timeout_urls and not await self._check_if_killed():
            logger.info(f"Starting second pass for {len(timeout_urls)} timed-out URLs with extended timeout")
            
            # Mark all timeout URLs as pending for retry
            for url in timeout_urls:
                await self._mark_url_for_retry(url)
            
            # Process URLs with extended timeout - second pass
            for url in timeout_urls:
                # Check if job has been killed
                if await self._check_if_killed():
                    logger.info(f"Job {self.job_id} was killed during retry phase. Stopping.")
                    break
                
                # Process the URL with extended timeout
                success = await self.crawl_url(url, is_retry=True)
                
                # Only update progress if we had a new success
                if success:
                    await self._update_job_progress(success)
                
                # Use a slower rate for retries to be extra careful
                retry_rate = max(0.5, self.current_rate * 0.5)
                await asyncio.sleep(1 / retry_rate)
        
        # Job completed - only mark as completed if it wasn't killed
        if not await self._check_if_killed():
            await self._update_job_status('completed')
            
    @sync_to_async
    def get_current_url_status(self):
        """Get the status of the currently processing URL"""
        if self.current_url_id:
            try:
                url = CrawledURL.objects.get(id=self.current_url_id)
                return {
                    'id': url.id,
                    'url': url.url,
                    'status': url.retry_status,
                    'screenshot': self.page_screenshot,
                }
            except CrawledURL.DoesNotExist:
                return None
        return None

    async def _extract_content(self, page):
        """Extract structured content from a page"""
        try:
            # Basic structured data
            structured_data = {
                'title': await page.title(),
                'url': page.url,
                'timestamp': timezone.now().isoformat(),
                'text_content': '',
                'meta': {},
                'links': [],
                'images': [],
                'tables': []
            }
            
            # Extract meta tags
            meta_tags = await page.evaluate("""() => {
                const metaTags = {};
                document.querySelectorAll('meta').forEach(tag => {
                    const name = tag.getAttribute('name') || tag.getAttribute('property');
                    const content = tag.getAttribute('content');
                    if (name && content) {
                        metaTags[name] = content;
                    }
                });
                return metaTags;
            }""")
            structured_data['meta'] = meta_tags
            
            # Extract main text content (visible text nodes)
            text_content = await page.evaluate("""() => {
                function getVisibleText(node) {
                    let text = '';
                    if (node.nodeType === Node.TEXT_NODE) {
                        return node.textContent.trim();
                    } else if (node.nodeType === Node.ELEMENT_NODE) {
                        const style = window.getComputedStyle(node);
                        const isHidden = style.display === 'none' || style.visibility === 'hidden';
                        if (!isHidden) {
                            if (node.tagName === 'P' || node.tagName === 'H1' || node.tagName === 'H2' || 
                                node.tagName === 'H3' || node.tagName === 'H4' || node.tagName === 'H5' || 
                                node.tagName === 'DIV' || node.tagName === 'SPAN' || node.tagName === 'LI') {
                                let nodeText = '';
                                for (let child of node.childNodes) {
                                    nodeText += ' ' + getVisibleText(child);
                                }
                                return nodeText.trim();
                            } else {
                                let childrenText = '';
                                for (let child of node.childNodes) {
                                    childrenText += ' ' + getVisibleText(child);
                                }
                                return childrenText.trim();
                            }
                        }
                    }
                    return '';
                }
                
                // Get visible text from body, skipping script and style elements
                const text = getVisibleText(document.body)
                    .replace(/\\s+/g, ' ')
                    .trim();
                return text;
            }""")
            structured_data['text_content'] = text_content
            
            # Extract links
            links = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('a[href]')).map(a => {
                    return {
                        text: a.textContent.trim(),
                        href: a.href,
                        title: a.title || '',
                    };
                });
            }""")
            structured_data['links'] = links
            
            # Extract images
            images = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('img')).map(img => {
                    return {
                        src: img.src,
                        alt: img.alt || '',
                        width: img.width,
                        height: img.height
                    };
                });
            }""")
            structured_data['images'] = images
            
            # Extract tables
            tables = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('table')).map(table => {
                    const rows = Array.from(table.querySelectorAll('tr')).map(row => {
                        const cells = Array.from(row.querySelectorAll('td, th')).map(cell => {
                            return cell.textContent.trim();
                        });
                        return cells;
                    });
                    return rows;
                });
            }""")
            structured_data['tables'] = tables
            
            return structured_data
        except Exception as e:
            logger.error(f"Error extracting content: {str(e)}")
            return None

class ParallelCrawlerService:
    """Service for crawling URLs with multiple workers and proxy rotation"""
    
    def __init__(self, job_id, debug_mode=False, worker_count=3):
        self.job_id = job_id
        self.job = None
        self.worker_count = max(1, min(worker_count, 20))  # Ensure worker count is between 1 and 20
        self.debug_mode = debug_mode
        self.workers = []  # Will store worker instances
        self.used_proxies = []  # For tracking used proxies in round-robin mode
        
    @sync_to_async
    def _init_job(self):
        """Initialize job object"""
        self.job = CrawlJob.objects.get(id=self.job_id)
        logger.info(f"Initialized parallel job {self.job_id} with {self.worker_count} workers")
        
    @sync_to_async
    def _update_job_status(self, status, cooloff_until=None):
        """Update job status in async context"""
        self.job.status = status
        if cooloff_until:
            self.job.cooloff_until = cooloff_until
        self.job.save(update_fields=['status', 'cooloff_until'] if cooloff_until else ['status'])
    
    @sync_to_async
    def _get_pending_urls(self):
        """Get a batch of pending URLs"""
        # Get URLs that haven't been processed yet
        return list(CrawledURL.objects.filter(
            job_id=self.job_id,
            content__isnull=True,
            retry_status='pending'
        ).values_list('id', flat=True)[:100])
    
    @sync_to_async
    def _get_timeout_urls(self):
        """Get URLs that timed out and need retry"""
        return list(CrawledURL.objects.filter(job_id=self.job_id, retry_status='timeout').values_list('id', flat=True))
    
    @sync_to_async
    def _mark_url_for_retry(self, url_id):
        """Mark URL for retry in async context"""
        CrawledURL.objects.filter(id=url_id).update(retry_status='retry_pending')
    
    @sync_to_async
    def _check_if_killed(self):
        """Check if job has been killed in async context"""
        job = CrawlJob.objects.get(id=self.job_id)
        return job.status == 'killed'
    
    @sync_to_async
    def _update_job_progress(self, success):
        """Update job progress in async context"""
        if success:
            # Use F() to handle concurrent updates
            CrawlJob.objects.filter(id=self.job_id).update(urls_processed=F('urls_processed') + 1)
    
    @sync_to_async
    def _init_stats(self):
        """Initialize stats object for parallel job"""
        stats, created = CrawlStats.objects.get_or_create(job_id=self.job_id)
        if created:
            logger.info(f"Created new stats for job {self.job_id}")
        return stats
    
    @sync_to_async
    def _get_available_proxy(self):
        """Get an available proxy that respects job settings"""
        countries = self.job.proxy_countries if self.job else None
        reshuffle = self.job.reshuffle_proxies if self.job else False
        
        proxy = WebshareProxyService.get_available_proxy(
            countries=countries,
            reshuffle=reshuffle,
            used_proxies=self.used_proxies if reshuffle else None
        )
        
        # Track used proxies for round-robin mode
        if proxy and reshuffle and proxy.id not in self.used_proxies:
            self.used_proxies.append(proxy.id)
            
        return proxy
    
    async def worker(self, worker_id):
        """Worker process that crawls URLs"""
        logger.info(f"Starting worker {worker_id} for job {self.job_id}")
        
        # Create a worker-specific crawler service
        worker_service = CrawlerService(self.job_id, debug_mode=self.debug_mode)
        
        # Initialize the worker service
        await worker_service._init_job_and_stats()
        
        # If we're using reshuffle mode, override the worker's proxy selection
        # to use our centralized proxy management
        if self.job and self.job.reshuffle_proxies:
            # Replace the worker's _get_available_proxy method with our own
            worker_service._original_get_proxy = worker_service._get_available_proxy
            worker_service._get_available_proxy = self._get_available_proxy
        
        self.workers.append(worker_service)
        
        while True:
            # Check if the job has been killed
            if await self._check_if_killed():
                logger.info(f"Worker {worker_id} stopping because job was killed")
                break
                
            # Get an available URL to process
            url_id = None
            
            # Try to get a pending URL first
            pending_urls = await self._get_pending_urls()
            if pending_urls:
                url_id = pending_urls[0]
            
            # If no pending URLs, try timeout URLs
            if url_id is None:
                timeout_urls = await self._get_timeout_urls()
                if timeout_urls:
                    url_id = timeout_urls[0]
                    # Mark it as retry_pending so other workers don't grab it
                    await self._mark_url_for_retry(url_id)
            
            # If we have a URL to process, crawl it
            if url_id:
                try:
                    url = await sync_to_async(CrawledURL.objects.get)(id=url_id)
                    success = await worker_service.crawl_url(url)
                    
                    # Update the main job's progress counter
                    if success:
                        await self._update_job_progress(True)
                        
                except Exception as e:
                    logger.exception(f"Worker {worker_id} error processing URL {url_id}: {str(e)}")
            else:
                # No URLs to process, check if we're done
                if await sync_to_async(self.job.__class__.objects.get)(id=self.job_id).urls_processed >= await sync_to_async(lambda: self.job.urls_total)():
                    logger.info(f"Worker {worker_id} finishing - all URLs processed")
                    break
                
                # Wait briefly before checking again
                await asyncio.sleep(1)
        
        logger.info(f"Worker {worker_id} finished for job {self.job_id}")
    
    async def retry_processor(self):
        """Process URLs that need to be retried"""
        logger.info(f"Starting retry processor for job {self.job_id}")
        
        while True:
            # Check if the job has been killed
            if await self._check_if_killed():
                logger.info(f"Retry processor stopping because job was killed")
                break
            
            # Get URLs that need to be retried
            retry_urls = await sync_to_async(list)(CrawledURL.objects.filter(
                job_id=self.job_id,
                retry_status='retry_pending'
            ))
            
            if retry_urls:
                logger.info(f"Found {len(retry_urls)} URLs to retry for job {self.job_id}")
                
                # Pick an available worker
                if self.workers:
                    # Simple round-robin for now
                    for i, url in enumerate(retry_urls):
                        worker = self.workers[i % len(self.workers)]
                        await worker.crawl_url(url, is_retry=True)
            
            # Wait before checking again
            await asyncio.sleep(5)
        
        logger.info(f"Retry processor finished for job {self.job_id}")
    
    async def process_job(self):
        """Main method to process a parallel crawl job"""
        # Initialize the job
        await self._init_job()
        
        # Update job status to running
        await self._update_job_status('running')
        
        try:
            # Initialize stats
            await self._init_stats()
            
            # Create worker tasks
            worker_tasks = []
            for i in range(self.worker_count):
                worker_tasks.append(asyncio.create_task(self.worker(i + 1)))
            
            # Create retry processor task
            retry_task = asyncio.create_task(self.retry_processor())
            
            # Wait for all workers to complete
            await asyncio.gather(*worker_tasks)
            
            # Cancel the retry processor when workers are done
            retry_task.cancel()
            
            # Mark job as completed if all URLs have been processed
            job = await sync_to_async(CrawlJob.objects.get)(id=self.job_id)
            if job.urls_processed >= job.urls_total:
                await self._update_job_status('completed')
            else:
                await self._update_job_status('failed')
                
        except Exception as e:
            logger.exception(f"Error in parallel job {self.job_id}: {str(e)}")
            await self._update_job_status('failed')
            
        logger.info(f"Parallel job {self.job_id} finished") 