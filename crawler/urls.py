from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('dashboard/<int:job_id>/', views.dashboard, name='dashboard'),
    path('dashboard/<int:job_id>/kill/', views.kill_job, name='kill_job'),
    path('dashboard/<int:job_id>/reset/', views.reset_job, name='reset_job'),
    path('content/<int:url_id>/', views.content_view, name='content_view'),
    path('sync-proxies/', views.sync_proxies, name='sync_proxies'),
    path('api/job-stats/<int:job_id>/', views.job_stats, name='job_stats'),
    path('api/browser-preview/<int:job_id>/', views.browser_preview, name='browser_preview'),
    path('api/export-progress/<int:job_id>/', views.export_progress, name='export_progress'),
    path('export/url/<int:url_id>/structured/', views.export_url_content, {'content_type': 'structured'}, name='export_url_structured'),
    path('export/url/<int:url_id>/raw/', views.export_url_content, {'content_type': 'raw'}, name='export_url_raw'),
    path('export/job/<int:job_id>/structured/', views.export_job_content, {'content_type': 'structured'}, name='export_job_structured'),
    path('export/job/<int:job_id>/raw/', views.export_job_content, {'content_type': 'raw'}, name='export_job_raw'),
    path('proxies/', views.proxy_list, name='proxy_list'),
] 