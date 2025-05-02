# InHouse Crawler with Proxy Rotation

A custom web crawler with intelligent proxy rotation to maximize the number of pages fetched without getting rate-limited or blocked.

## Features

- URL crawling with Playwright headless browser
- WebShare API integration for proxy management
- Intelligent proxy rotation and rate limiting
- Rate limit detection and automatic adjustment
- Cooloff period when all proxies are blocked
- Live monitoring of crawl jobs
- Content viewing of crawled pages

## Technical Stack

- Django web framework
- Playwright for headless browser automation
- WebShare for proxy IPs
- SQLite database for content storage
- Async/await architecture
- Modern responsive UI with Bootstrap

## Setup Instructions

### Prerequisites

- Python 3.8+
- pip (Python package manager)
- virtual environment (recommended)

### Installation

1. Clone the repository:
```
git clone https://github.com/yourusername/inhousecrawler.git
cd inhousecrawler
```

2. Create and activate a virtual environment:
```
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install the required packages:
```
pip install -r requirements.txt
```

4. Install Playwright browsers:
```
playwright install
```

5. Set up the database:
```
python manage.py migrate
```

6. Create a superuser:
```
python manage.py createsuperuser
```

7. Run the development server:
```
python manage.py runserver
```

8. Access the application at http://127.0.0.1:8000/

## Usage

1. **Sync Proxies**: Before starting a crawl job, sync proxies from WebShare by clicking the "Sync Proxies" button in the navigation bar.

2. **Submit URLs**: On the home page, submit a list of URLs to crawl (one per line).

3. **Start Crawling**: On the job dashboard, click "Start Crawling" to begin the process.

4. **Monitor Progress**: Watch the live statistics on the dashboard to see the progress, current proxy, rate information, and more.

5. **View Content**: Click on any crawled URL in the list to view its content.

## Configuration

The WebShare API key and email are configured in the settings.py file. For production, it's recommended to use environment variables for these sensitive values.

## Proxy Rotation Logic

The crawler employs the following strategy for proxy rotation:

1. Starts conservatively with 1 request per second
2. Gradually increases the request rate for successful crawls
3. Detects rate limiting or blocking based on response content hashes and errors
4. Automatically adjusts the request rate when rate limiting is detected
5. Rotates to a new proxy when the current one is blocked
6. Enters a cooloff period if all proxies are blocked
7. Unblocks proxies after the cooloff period (5 minutes by default)

## License

MIT License 