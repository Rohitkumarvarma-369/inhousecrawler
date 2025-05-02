from django import forms
from .models import CrawlJob
from .services import WebshareProxyService

class URLSubmissionForm(forms.Form):
    urls = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 10, 'placeholder': 'Enter URLs, one per line'}),
        help_text='Enter one URL per line'
    )
    
    proxy_countries = forms.MultipleChoiceField(
        required=False,
        widget=forms.SelectMultiple(attrs={'class': 'form-select', 'size': '5'}),
        help_text='Select countries for proxy filtering (optional). Leave empty to use all available proxies.'
    )
    
    reshuffle_proxies = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        help_text='Enable round-robin proxy rotation. Useful for large sites to avoid detection.'
    )
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Dynamically populate country choices
        countries = WebshareProxyService.get_available_countries()
        country_choices = [(c, c) for c in countries]
        self.fields['proxy_countries'].choices = country_choices
    
    def clean_urls(self):
        data = self.cleaned_data['urls']
        urls = [url.strip() for url in data.split('\n') if url.strip()]
        
        if not urls:
            raise forms.ValidationError("Please enter at least one valid URL")
        
        # Basic URL validation
        for url in urls:
            if not url.startswith(('http://', 'https://')):
                raise forms.ValidationError(f"Invalid URL format: {url}. URLs must start with http:// or https://")
        
        return urls
        
    def clean_proxy_countries(self):
        """Convert list of country codes to comma-separated string"""
        countries = self.cleaned_data.get('proxy_countries', [])
        if countries:
            return ','.join(countries)
        return None 