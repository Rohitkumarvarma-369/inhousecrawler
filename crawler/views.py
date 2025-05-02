from django.shortcuts import render
import json
import asyncio
import threading
import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.contrib import messages
from django.urls import reverse
from django.db.models import F
from django.conf import settings
from asgiref.sync import sync_to_async
from .models import CrawlJob, CrawledURL, CrawlStats, Proxy
from .forms import URLSubmissionForm
from .services import WebshareProxyService, CrawlerService
import time
from datetime import datetime

logger = logging.getLogger(__name__)

def home(request):
    """Home page with URL submission form"""
    if request.method == 'POST':
        form = URLSubmissionForm(request.POST)
        if form.is_valid():
            # Create a new crawl job
            debug_mode = 'debug_mode' in request.POST
            
            # Get parallel workers (default to 1 if not valid)
            try:
                parallel_workers = int(request.POST.get('parallel_workers', 1))
                # Ensure it's between 1 and 20
                parallel_workers = max(1, min(20, parallel_workers))
            except (ValueError, TypeError):
                parallel_workers = 1
                
            job = CrawlJob.objects.create(
                status='pending',
                debug_mode=debug_mode,
                parallel_workers=parallel_workers,
                proxy_countries=form.cleaned_data.get('proxy_countries'),
                reshuffle_proxies=form.cleaned_data.get('reshuffle_proxies', False)
            )
            
            # Create CrawledURL objects for each URL
            urls = form.cleaned_data['urls']
            job.urls_total = len(urls)
            job.save()
            
            # Create CrawledURL objects
            crawled_urls = [
                CrawledURL(job=job, url=url)
                for url in urls
            ]
            CrawledURL.objects.bulk_create(crawled_urls)
            
            # Create stats object
            CrawlStats.objects.create(job=job)
            
            # Redirect to the dashboard
            return redirect('dashboard', job_id=job.id)
    else:
        form = URLSubmissionForm()
    
    # Get recent jobs for display
    recent_jobs = CrawlJob.objects.order_by('-created_at')[:5]
    
    return render(request, 'crawler/home.html', {
        'form': form,
        'recent_jobs': recent_jobs,
    })

def dashboard(request, job_id):
    """Dashboard for monitoring a crawl job"""
    job = get_object_or_404(CrawlJob, id=job_id)
    
    # Check if we need to start the job
    if job.status == 'pending' and 'start' in request.GET:
        # Start the job asynchronously
        # In a production environment, this would use Celery or similar
        # For simplicity, we'll run it in a separate thread
        def run_job():
            # Create a new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                # Run the crawler in this thread's event loop
                # Use the parallel crawler service if parallel_workers > 1
                if job.parallel_workers > 1:
                    from .services import ParallelCrawlerService
                    loop.run_until_complete(ParallelCrawlerService(
                        job_id, 
                        debug_mode=job.debug_mode,
                        worker_count=job.parallel_workers
                    ).process_job())
                else:
                    # Use the regular crawler service for a single worker
                    loop.run_until_complete(CrawlerService(
                        job_id, 
                        debug_mode=job.debug_mode
                    ).process_job())
            except Exception as e:
                logger.exception(f"Error running crawler: {str(e)}")
            finally:
                loop.close()
        
        thread = threading.Thread(target=run_job)
        thread.daemon = True
        thread.start()
        
        return redirect('dashboard', job_id=job.id)
    
    # Get related data
    crawled_urls = job.urls.all().order_by('-crawled_at')[:100]
    
    return render(request, 'crawler/dashboard.html', {
        'job': job,
        'crawled_urls': crawled_urls,
    })

def kill_job(request, job_id):
    """Kill a running crawl job"""
    if request.method == 'POST':
        job = get_object_or_404(CrawlJob, id=job_id)
        
        # Only kill jobs that are running or in cooloff
        if job.status in ['running', 'cooloff']:
            job.kill()
            messages.success(request, f'Job #{job_id} has been killed')
        else:
            messages.warning(request, f'Job #{job_id} is not running (current status: {job.get_status_display()})')
            
    return redirect('dashboard', job_id=job_id)

def reset_job(request, job_id):
    """Reset a job to its initial state"""
    if request.method == 'POST':
        job = get_object_or_404(CrawlJob, id=job_id)
        
        # Only reset jobs that aren't running
        if job.status not in ['running', 'cooloff']:
            job.reset()
            messages.success(request, f'Job #{job_id} has been reset and is ready to start')
        else:
            messages.error(request, f'Cannot reset job #{job_id} while it is running. Kill it first.')
    
    return redirect('dashboard', job_id=job_id)

def content_view(request, url_id):
    """View the content of a crawled URL"""
    crawled_url = get_object_or_404(CrawledURL, id=url_id)
    
    # Parse structured content if available
    structured_content = None
    if crawled_url.structured_content:
        try:
            structured_content = json.loads(crawled_url.structured_content)
        except json.JSONDecodeError:
            # Handle invalid JSON
            pass
    
    return render(request, 'crawler/content.html', {
        'crawled_url': crawled_url,
        'structured_content': structured_content,
    })

def sync_proxies(request):
    """Sync proxies from WebShare API"""
    if request.method == 'POST':
        count = WebshareProxyService.sync_proxies()
        messages.success(request, f'Successfully synced {count} proxies')
    
    return redirect('home')

@csrf_exempt
def job_stats(request, job_id):
    """API endpoint for getting job stats"""
    job = get_object_or_404(CrawlJob, id=job_id)
    
    try:
        stats = job.stats
    except CrawlStats.DoesNotExist:
        stats = CrawlStats.objects.create(job=job)
    
    # Calculate cooloff remaining time
    cooloff_remaining = None
    if job.status == 'cooloff' and job.cooloff_until:
        now = timezone.now()
        if job.cooloff_until > now:
            cooloff_remaining = (job.cooloff_until - now).total_seconds()
    
    # Get current proxy info
    current_proxy = None
    if stats.current_proxy:
        current_proxy = {
            'ip': stats.current_proxy.ip_address,
            'port': stats.current_proxy.port,
            'country': stats.current_proxy.country_code,
        }
    
    # Get active proxies being used by recent URLs
    active_proxies = []
    if job.parallel_workers > 1:
        # Get recent crawled URLs with their proxies
        recent_urls = CrawledURL.objects.filter(
            job=job, 
            proxy_used__isnull=False,
            crawled_at__isnull=False
        ).order_by('-crawled_at')[:job.parallel_workers*2]
        
        # Extract unique proxies
        seen_ips = set()
        for url in recent_urls:
            if url.proxy_used and url.proxy_used.ip_address not in seen_ips:
                seen_ips.add(url.proxy_used.ip_address)
                active_proxies.append({
                    'ip': url.proxy_used.ip_address,
                    'port': url.proxy_used.port,
                    'country': url.proxy_used.country_code,
                })
                if len(active_proxies) >= job.parallel_workers:
                    break
    
    # Blocked proxies count
    blocked_proxies = Proxy.objects.filter(is_blocked=True).count()
    
    # Get retry statistics
    timeout_urls = job.urls.filter(retry_status='timeout').count()
    retry_pending_urls = job.urls.filter(retry_status='retry_pending').count()
    failed_urls = job.urls.filter(retry_status='failed').count()
    
    data = {
        'id': job.id,
        'status': job.status,
        'urls_total': job.urls_total,
        'urls_processed': job.urls_processed,
        'progress_percent': round((job.urls_processed / job.urls_total * 100) if job.urls_total > 0 else 0, 1),
        'rate_limit_hits': job.rate_limit_hits,
        'current_rate': job.current_rate,
        'successful_requests': stats.successful_requests,
        'failed_requests': stats.failed_requests,
        'avg_response_time': round(stats.avg_response_time, 3) if stats.avg_response_time else None,
        'cooloff_remaining': cooloff_remaining,
        'blocked_proxies_count': blocked_proxies,
        'current_proxy': current_proxy,
        'active_proxies': active_proxies,
        'last_updated': stats.last_request_time.isoformat() if stats.last_request_time else None,
        'timeout_urls': timeout_urls,
        'retry_pending_urls': retry_pending_urls,
        'failed_urls': failed_urls,
        'debug_mode': job.debug_mode,
        'parallel_workers': job.parallel_workers,
    }
    
    return JsonResponse(data)

@csrf_exempt
def browser_preview(request, job_id):
    """API endpoint for getting browser preview data"""
    job = get_object_or_404(CrawlJob, id=job_id)
    
    if not job.debug_mode:
        return JsonResponse({'error': 'Debug mode not enabled for this job'}, status=400)
    
    # This would be implemented properly with a shared service instance
    # For now, we'll use a dummy implementation that returns preview data from the database
    
    # Get the URL currently being processed (if any)
    current_url = CrawledURL.objects.filter(
        job=job,
        retry_status__in=['pending', 'retry_pending'],
        content__isnull=True
    ).first()
    
    if job.status == 'running' and current_url:
        # Get the most recent screenshot for this URL
        data = {
            'url': current_url.url,
            'id': current_url.id,
            'status': current_url.retry_status,
            'screenshot_path': current_url.screenshot_path,
        }
    else:
        data = {
            'url': None,
            'id': None,
            'status': None,
            'screenshot_path': None,
        }
    
    return JsonResponse(data)

def proxy_list(request):
    """View the list of proxies"""
    proxies = Proxy.objects.all().order_by('-last_used')
    
    return render(request, 'crawler/proxies.html', {
        'proxies': proxies,
    })

def export_url_content(request, url_id, content_type='structured'):
    """Export URL content as JSON file"""
    crawled_url = get_object_or_404(CrawledURL, id=url_id)
    
    filename = f"url_{url_id}_{content_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    if content_type == 'structured' and crawled_url.structured_content:
        # Export structured content
        response_data = crawled_url.structured_content
        content_disposition = f'attachment; filename="{filename}"'
    else:
        # Export raw HTML as JSON
        response_data = json.dumps({
            'url': crawled_url.url,
            'status_code': crawled_url.status_code,
            'crawled_at': crawled_url.crawled_at.isoformat() if crawled_url.crawled_at else None,
            'content': crawled_url.content,
            'content_hash': crawled_url.content_hash,
        }, ensure_ascii=False)
        content_disposition = f'attachment; filename="url_{url_id}_raw_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json"'
    
    response = HttpResponse(response_data, content_type='application/json')
    response['Content-Disposition'] = content_disposition
    return response

def export_job_content(request, job_id, content_type='structured'):
    """Export all URLs content from a job as streaming JSON response"""
    job = get_object_or_404(CrawlJob, id=job_id)
    
    # Generate filename
    filename = f"job_{job_id}_{content_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    # Check if we should stream the response for large datasets
    count = job.urls.count()
    use_streaming = count > 50  # Use streaming for jobs with more than 50 URLs
    
    if use_streaming:
        # Use streaming response for large jobs
        response = StreamingHttpResponse(
            _stream_job_content(job, content_type),
            content_type='application/json'
        )
    else:
        # Regular response for smaller jobs
        if content_type == 'structured':
            # Export all structured content as a list
            data = []
            for url in job.urls.filter(structured_content__isnull=False):
                try:
                    url_data = json.loads(url.structured_content)
                    data.append(url_data)
                except json.JSONDecodeError:
                    continue
            response = HttpResponse(
                json.dumps(data, ensure_ascii=False),
                content_type='application/json'
            )
        else:
            # Export all raw HTML as a list of objects
            data = []
            for url in job.urls.all():
                data.append({
                    'url': url.url,
                    'status_code': url.status_code,
                    'crawled_at': url.crawled_at.isoformat() if url.crawled_at else None,
                    'content': url.content,
                    'content_hash': url.content_hash,
                })
            response = HttpResponse(
                json.dumps(data, ensure_ascii=False),
                content_type='application/json'
            )
    
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response

def _stream_job_content(job, content_type):
    """Generator function to stream large job content"""
    # Start with opening bracket
    yield '['
    
    urls = job.urls.all()
    total = urls.count()
    
    for i, url in enumerate(urls):
        if content_type == 'structured' and url.structured_content:
            try:
                # Just use the structured content directly (it's already JSON)
                yield url.structured_content if i == 0 else ',' + url.structured_content
            except:
                continue
        else:
            # Create JSON for raw content
            data = {
                'url': url.url,
                'status_code': url.status_code,
                'crawled_at': url.crawled_at.isoformat() if url.crawled_at else None,
                'content': url.content,
                'content_hash': url.content_hash,
            }
            if i == 0:
                yield json.dumps(data, ensure_ascii=False)
            else:
                yield ',' + json.dumps(data, ensure_ascii=False)
        
        # Every 10 items, yield a newline for readability
        if i % 10 == 0 and i > 0:
            yield '\n'
    
    # End with closing bracket
    yield ']'

@csrf_exempt
def export_progress(request, job_id):
    """API endpoint for getting export progress"""
    job = get_object_or_404(CrawlJob, id=job_id)
    
    # For structured content export, only count URLs that have structured_content
    export_type = request.GET.get('type', 'raw')
    if export_type == 'structured':
        total_urls = job.urls.count()
        processed_urls = job.urls.filter(structured_content__isnull=False).count()
    else:
        total_urls = job.urls.count()
        processed_urls = job.urls.filter(content__isnull=False).count()
    
    data = {
        'total': total_urls,
        'processed': processed_urls,
        'progress_percent': round((processed_urls / total_urls * 100) if total_urls > 0 else 0, 1),
    }
    
    return JsonResponse(data)
