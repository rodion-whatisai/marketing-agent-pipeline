# -*- coding: utf-8 -*-
"""СНАПШОТ view-таблиц реестра platforms.py (перегенерирован после шага B, 2026-07-13).
Детектор СЛУЧАЙНЫХ изменений: при сознательной правке реестра перегенерировать
(см. шапку platforms.py) и закоммитить вместе с ней. НЕ ПРАВИТЬ РУКАМИ."""

FROZEN_PIXEL_RULES = {'Meta': {'domains': ['facebook.com/tr',
                      'connect.facebook.net/en_US/fbevents',
                      'connect.facebook.net/signals/config'],
          'event_param': 'ev',
          'id_param': 'id',
          'id_path_re': '/signals/config/(\\d{6,})'},
 'Google Analytics': {'domains': ['analytics.google.com/g/collect',
                                  'google-analytics.com/collect',
                                  'google-analytics.com/g/collect'],
                      'event_param': 'en',
                      'id_param': 'tid'},
 'Google Ads': {'domains': ['googleadservices.com/pagead/conversion',
                            'google.com/pagead/1p-conversion',
                            'google.com/ccm/collect',
                            'doubleclick.net/ccm/s/collect',
                            'doubleclick.net/pagead/viewthroughconversion',
                            'pagead/1p-user-list'],
                'event_param': 'en',
                'id_path_re': '/(?:conversion|viewthroughconversion)/(\\d{6,})'},
 'Bing/Microsoft': {'domains': ['bat.bing.com/action', 'bat.bing.com/p/action'],
                    'event_param': 'ea',
                    'id_param': 'ti'},
 'LinkedIn': {'domains': ['px.ads.linkedin.com', 'snap.licdn.com'],
              'event_param': 'conversionId',
              'id_param': 'pid'},
 'TikTok': {'domains': ['tiktok.com/api/v2/pixel', 'tiktok.com/i18n/pixel/'],
            'event_param': 'event',
            'id_param': 'sdkid'},
 'Snapchat': {'domains': ['tr.snapchat.com', 'sc-static.net/scevent'], 'event_param': None},
 'Pinterest': {'domains': ['ct.pinterest.com'], 'event_param': 'event', 'id_param': 'tid'}}

FROZEN_PIXEL_RULES_KEY_ORDER = ['Meta', 'Google Analytics', 'Google Ads', 'Bing/Microsoft', 'LinkedIn', 'TikTok', 'Snapchat', 'Pinterest']

FROZEN_TIER1 = {'Meta': ['Purchase',
          'Lead',
          'InitiateCheckout',
          'AddToCart',
          'CompleteRegistration',
          'Schedule',
          'Contact',
          'AddPaymentInfo'],
 'Google Analytics': ['purchase',
                      'begin_checkout',
                      'add_to_cart',
                      'generate_lead',
                      'form_submit',
                      'conversion'],
 'Google Ads': ['conversion'],
 'Bing/Microsoft': ['purchase', 'lead', 'conversion'],
 'TikTok': ['Purchase', 'AddToCart', 'InitiateCheckout', 'PlaceAnOrder'],
 'Snapchat': ['PURCHASE', 'START_CHECKOUT', 'ADD_CART', 'SIGN_UP', 'LEAD'],
 'Pinterest': ['checkout', 'addtocart', 'signup', 'lead']}

FROZEN_TIER1_KEY_ORDER = ['Meta', 'Google Analytics', 'Google Ads', 'Bing/Microsoft', 'TikTok', 'Snapchat', 'Pinterest']

FROZEN_TIER2 = {'Meta': ['ViewContent', 'Search', 'Subscribe'],
 'Google Analytics': ['view_item', 'view_item_list', 'search', 'select_item', 'view_promotion'],
 'Google Ads': [],
 'Bing/Microsoft': [],
 'TikTok': ['ViewContent'],
 'Pinterest': ['viewcategory', 'search', 'watchvideo']}

FROZEN_TIER2_KEY_ORDER = ['Meta', 'Google Analytics', 'Google Ads', 'Bing/Microsoft', 'TikTok', 'Pinterest']

FROZEN_NOISE = {'Meta': ['fired'],
 'Google Analytics': ['gtm.init',
                      'gtm.init_consent',
                      'gtm.js',
                      'fired',
                      'page_view',
                      'user_engagement',
                      'session_start',
                      'first_visit',
                      'scroll',
                      'click',
                      'view_item_list',
                      'form_start',
                      'form_close'],
 'Google Ads': ['page_view', 'gtag.config'],
 'Bing/Microsoft': ['fired'],
 'TikTok': ['fired'],
 'LinkedIn': ['fired'],
 'Snapchat': ['fired'],
 'Pinterest': ['fired', 'init']}

FROZEN_NOISE_KEY_ORDER = ['Meta', 'Google Analytics', 'Google Ads', 'Bing/Microsoft', 'TikTok', 'LinkedIn', 'Snapchat', 'Pinterest']

FROZEN_SHOPIFY_APP_IDS = {'550306007': 'Meta',
 '2179629271': 'Google Analytics',
 '96403671': 'TikTok',
 '136216791': 'Pinterest'}

FROZEN_SHOPIFY_APP_IDS_KEY_ORDER = ['550306007', '2179629271', '96403671', '136216791']

FROZEN_SHOPIFY_MARKERS = {'Meta': ('fbevents.js', 'fbq(', 'connect.facebook.net', 'facebook.com/tr'),
 'Google Analytics': ('googletagmanager.com', 'google-analytics.com', 'gtag('),
 'TikTok': ('ttq.load', 'analytics.tiktok.com'),
 'Pinterest': ('pintrk', 'ct.pinterest.com', 's.pinimg.com'),
 'Bing/Microsoft': ('bat.bing.com', 'uetq'),
 'LinkedIn': ('lintrk', 'snap.licdn.com', 'px.ads.linkedin.com'),
 'Snapchat': ('snaptr(', 'tr.snapchat.com', 'sc-static.net')}

FROZEN_SHOPIFY_MARKERS_KEY_ORDER = ['Meta', 'Google Analytics', 'TikTok', 'Pinterest', 'Bing/Microsoft', 'LinkedIn', 'Snapchat']

FROZEN_GTM_SIGNATURES = {'Meta Pixel': ['fbq\\s*\\(',
                'connect\\.facebook\\.net',
                'fbevents\\.js',
                'facebook\\.com/tr',
                'Meta Pixel'],
 'Google Analytics GA4': ['G-[A-Z0-9]{6,}',
                          'gtag\\s*\\(',
                          'analytics\\.google\\.com',
                          'google-analytics\\.com/g/collect'],
 'Google Ads': ['AW-\\d{6,}', 'googleadservices\\.com', 'conversion_id', 'google\\.com/pagead'],
 'LinkedIn Insight': ['snap\\.licdn\\.com',
                      'linkedin\\.com/li',
                      '_linkedin_partner_id',
                      'px\\.ads\\.linkedin\\.com'],
 'TikTok Pixel': ['analytics\\.tiktok\\.com', 'ttq\\.', 'TiktokAnalyticsObject'],
 'Hotjar': ['hotjar\\.com', 'hjid\\s*[:=]', 'hj\\s*\\(', 'hjSetting'],
 'Microsoft/Bing': ['bat\\.bing\\.com', 'uetq\\s*=', 'bing\\.com/action'],
 'Intercom': ['intercom\\.com', 'intercomSettings'],
 'HubSpot': ['hubspot\\.com', 'hs-scripts', 'hbspt\\.'],
 'Drift': ['drift\\.com', 'driftt\\.com'],
 'Zendesk': ['zendesk\\.com', 'zopim'],
 'Clarity': ['clarity\\.ms', 'microsoft\\.com/clarity'],
 'Segment': ['segment\\.com', 'cdn\\.segment'],
 'Mixpanel': ['mixpanel\\.com', 'mixpanel\\.init'],
 'Amplitude': ['amplitude\\.com', 'amplitude\\.init'],
 'Klaviyo': ['klaviyo\\.com'],
 'Mailchimp': ['mailchimp\\.com', 'chimpstatic\\.com'],
 'Optimizely': ['optimizely\\.com'],
 'VWO': ['vwo\\.com', 'visualwebsiteoptimizer'],
 'Stripe': ['stripe\\.com', 'stripe\\.js'],
 'Crisp': ['crisp\\.chat'],
 'Freshchat': ['freshchat\\.com', 'freshworks\\.com'],
 'Snapchat Pixel': ['snaptr\\s*\\(', 'tr\\.snapchat\\.com', 'sc-static\\.net']}

FROZEN_GTM_SIGNATURES_KEY_ORDER = ['Meta Pixel', 'Google Analytics GA4', 'Google Ads', 'LinkedIn Insight', 'TikTok Pixel', 'Hotjar', 'Microsoft/Bing', 'Intercom', 'HubSpot', 'Drift', 'Zendesk', 'Clarity', 'Segment', 'Mixpanel', 'Amplitude', 'Klaviyo', 'Mailchimp', 'Optimizely', 'VWO', 'Stripe', 'Crisp', 'Freshchat', 'Snapchat Pixel']

