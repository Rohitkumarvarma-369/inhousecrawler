from django.contrib import admin
from .models import Proxy, CrawlJob, CrawledURL, CrawlStats

@admin.register(Proxy)
class ProxyAdmin(admin.ModelAdmin):
    list_display = ('ip_address', 'port', 'country_code', 'is_blocked', 'last_used')
    list_filter = ('is_blocked', 'country_code')
    search_fields = ('ip_address', 'country_code')

@admin.register(CrawlJob)
class CrawlJobAdmin(admin.ModelAdmin):
    list_display = ('id', 'status', 'urls_total', 'urls_processed', 'rate_limit_hits', 'current_rate', 'created_at')
    list_filter = ('status',)
    readonly_fields = ('created_at', 'updated_at')

@admin.register(CrawledURL)
class CrawledURLAdmin(admin.ModelAdmin):
    list_display = ('url', 'job', 'status_code', 'crawled_at', 'retry_count')
    list_filter = ('status_code', 'crawled_at', 'job')
    search_fields = ('url',)
    readonly_fields = ('content_hash',)

@admin.register(CrawlStats)
class CrawlStatsAdmin(admin.ModelAdmin):
    list_display = ('job', 'successful_requests', 'failed_requests', 'blocked_proxies_count', 'avg_response_time')
    readonly_fields = ('successful_requests', 'failed_requests', 'avg_response_time')
