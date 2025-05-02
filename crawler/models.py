from django.db import models
from django.utils import timezone

class Proxy(models.Model):
    ip_address = models.CharField(max_length=255)
    port = models.IntegerField()
    username = models.CharField(max_length=255)
    password = models.CharField(max_length=255)
    country_code = models.CharField(max_length=10, null=True, blank=True)
    is_blocked = models.BooleanField(default=False)
    blocked_at = models.DateTimeField(null=True, blank=True)
    last_used = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"{self.ip_address}:{self.port}"
    
    def mark_blocked(self):
        """Mark this proxy as blocked"""
        self.is_blocked = True
        self.blocked_at = timezone.now()
        self.save(update_fields=['is_blocked', 'blocked_at'])
    
    def mark_unblocked(self):
        """Mark this proxy as unblocked"""
        self.is_blocked = False
        self.blocked_at = None
        self.save(update_fields=['is_blocked', 'blocked_at'])

class CrawlJob(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cooloff', 'In Cooloff'),
        ('killed', 'Killed'),
    )
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    urls_total = models.IntegerField(default=0)
    urls_processed = models.IntegerField(default=0)
    rate_limit_hits = models.IntegerField(default=0)
    current_rate = models.FloatField(default=1.0)  # requests per second
    cooloff_until = models.DateTimeField(null=True, blank=True)
    debug_mode = models.BooleanField(default=False)  # Enable browser visibility/screenshots
    parallel_workers = models.IntegerField(default=1)  # Number of parallel IP addresses to use
    proxy_countries = models.CharField(max_length=100, null=True, blank=True)  # Comma-separated country codes for filtering proxies
    reshuffle_proxies = models.BooleanField(default=False)  # Enable round-robin proxy rotation
    
    def __str__(self):
        return f"Crawl Job {self.id} - {self.status}"
    
    def kill(self):
        """Kill the job (safely stop it)"""
        self.status = 'killed'
        self.save(update_fields=['status'])
        return True
    
    def reset(self):
        """Reset the job to pending state and clear all crawled data"""
        # Reset job state
        self.status = 'pending'
        self.urls_processed = 0
        self.rate_limit_hits = 0
        self.current_rate = 1.0
        self.cooloff_until = None
        self.save()
        
        # Reset stats
        try:
            stats = self.stats
            stats.current_proxy = None
            stats.blocked_proxies_count = 0
            stats.avg_response_time = 0
            stats.last_request_time = None
            stats.successful_requests = 0
            stats.failed_requests = 0
            stats.save()
        except CrawlStats.DoesNotExist:
            CrawlStats.objects.create(job=self)
        
        # Clear content from URLs
        self.urls.all().update(
            content=None,
            content_hash=None,
            status_code=None,
            crawled_at=None,
            proxy_used=None,
            retry_count=0,
            retry_status='pending',
            screenshot_path=None
        )
        
        return True

class CrawledURL(models.Model):
    RETRY_STATUS_CHOICES = (
        ('pending', 'Pending'),          # Initial state, no attempt yet
        ('success', 'Success'),          # Successfully crawled
        ('timeout', 'Timeout'),          # Timed out, needs retry
        ('retry_pending', 'Retry Pending'),  # Failed once, waiting for retry
        ('failed', 'Failed'),            # Failed after retry attempt
    )
    
    job = models.ForeignKey(CrawlJob, on_delete=models.CASCADE, related_name='urls')
    url = models.URLField(max_length=2000)
    content = models.TextField(null=True, blank=True)
    content_hash = models.CharField(max_length=64, null=True, blank=True)
    content_type = models.CharField(max_length=10, choices=[('html', 'HTML'), ('markdown', 'Markdown')], default='html')
    status_code = models.IntegerField(null=True, blank=True)
    crawled_at = models.DateTimeField(null=True, blank=True)
    proxy_used = models.ForeignKey(Proxy, on_delete=models.SET_NULL, null=True, blank=True, related_name='crawled_urls')
    retry_count = models.IntegerField(default=0)
    retry_status = models.CharField(max_length=15, choices=RETRY_STATUS_CHOICES, default='pending')
    screenshot_path = models.CharField(max_length=255, null=True, blank=True)  # Path to screenshot image
    structured_content = models.TextField(null=True, blank=True)  # JSON-formatted structured content
    
    def __str__(self):
        return self.url

class CrawlStats(models.Model):
    job = models.OneToOneField(CrawlJob, on_delete=models.CASCADE, related_name='stats')
    current_proxy = models.ForeignKey(Proxy, on_delete=models.SET_NULL, null=True, blank=True)
    blocked_proxies_count = models.IntegerField(default=0)
    avg_response_time = models.FloatField(default=0)
    last_request_time = models.DateTimeField(null=True, blank=True)
    successful_requests = models.IntegerField(default=0)
    failed_requests = models.IntegerField(default=0)
    
    def __str__(self):
        return f"Stats for {self.job}"
