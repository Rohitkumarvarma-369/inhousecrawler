import asyncio
import logging
from django.core.management.base import BaseCommand
from crawler.models import CrawlJob
from crawler.services import CrawlerService, WebshareProxyService

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Run the crawler for a specific job ID'

    def add_arguments(self, parser):
        parser.add_argument('job_id', type=int, help='ID of the crawl job to process')
        parser.add_argument('--sync-proxies', action='store_true', help='Sync proxies from WebShare before running the crawler')

    def handle(self, *args, **options):
        job_id = options['job_id']
        sync_proxies = options.get('sync_proxies', False)
        
        try:
            if sync_proxies:
                self.stdout.write(self.style.SUCCESS('Syncing proxies from WebShare...'))
                count = WebshareProxyService.sync_proxies()
                self.stdout.write(self.style.SUCCESS(f'Synced {count} proxies from WebShare'))
            
            job = CrawlJob.objects.get(id=job_id)
            self.stdout.write(self.style.SUCCESS(f'Starting crawler for job {job_id}'))
            
            # Run the crawler
            crawler = CrawlerService(job_id)
            asyncio.run(crawler.process_job())
            
            self.stdout.write(self.style.SUCCESS(f'Crawler completed for job {job_id}'))
            
        except CrawlJob.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'Job with id {job_id} does not exist'))
        except Exception as e:
            logger.exception(f"Error running crawler: {str(e)}")
            self.stdout.write(self.style.ERROR(f'Error running crawler: {str(e)}')) 