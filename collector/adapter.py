"""Versioned, explicit ScrapeSage mapping. No invented unsupported since_id."""
import copy
from datetime import datetime, timezone
from .storage import prepare_post
from .validation import ValidationError, parse_utc


class ScrapeSageAdapter:
    actor_id = 'scrapesage/twitter-scraper'
    build = 'unverified'
    supports_since_id = False

    def inputs(self, account, start, end, cursor):
        return {'handles': [account['current_handle']], 'includeProfile': True,
                'includeTweets': True, 'includeReplies': True, 'maxTweetsPerProfile': 500,
                'onlyTweetsNewerThan': start[:10], 'monitorMode': False}

    def normalize(self, raw, account, allow_text):
        if not isinstance(raw, dict):
            raise ValidationError('RAW_OBJECT_REQUIRED')
        if raw.get('type') == 'profile':
            return None
        if raw.get('type') not in (None, 'tweet'):
            raise ValidationError('RAW_TYPE')
        value = raw.get('createdAt')
        try:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError()
            published = stamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        except (ValueError, TypeError, AttributeError):
            raise ValidationError('RAW_TIME') from None
        author_id = raw.get('authorId')
        author = raw.get('authorHandle')
        if not isinstance(raw.get('id'), str) or not isinstance(author_id, str) or not isinstance(author, str):
            raise ValidationError('RAW_IDENTITY')
        kind = 'UNKNOWN'
        # Absence of type flags is UNKNOWN, never inferred ORIGINAL.
        if raw.get('isRetweet') is True:
            kind = 'REPOST'
        elif raw.get('isReply') is True:
            kind = 'REPLY'
        elif raw.get('isQuote') is True:
            kind = 'QUOTE'
        elif raw.get('isRetweet') is False and raw.get('isReply') is False and raw.get('isQuote') is False:
            kind = 'ORIGINAL'
        if allow_text and raw.get('text') is not None and not isinstance(raw['text'], str):
            raise ValidationError('RAW_TEXT')
        post = {'platform': 'X', 'post_id': raw['id'], 'account_id': account['account_id'],
                'author_external_id': author_id, 'author_handle_at_observation': author,
                'published_at': published, 'post_type': kind,
                'source_url': 'https://x.com/' + author + '/status/' + raw['id'],
                'quoted_post_id': raw.get('quotedTweetId'), 'in_reply_to_id': raw.get('inReplyToTweetId'),
                'reposted_post_id': raw.get('retweetedTweetId'), 'language': raw.get('lang'),
                'text': raw.get('text') if allow_text else None, 'media_refs': [], 'metrics': {},
                'hashtags': [], 'mentions': [], 'content_rating': 'UNKNOWN',
                'coverage_evidence_id': None, 'raw_ref': None}
        return prepare_post(post, account)


class StandardFixtureAdapter(ScrapeSageAdapter):
    actor_id = 'offline-synthetic'
    build = 'fixture-1.0'

    def normalize(self, raw, account, allow_text):
        row = copy.deepcopy(raw)
        if not allow_text and isinstance(row, dict):
            row['text'] = None
        return prepare_post(row, account)


class ActorRegistry:
    def __init__(self):
        self.adapters = {}

    def register(self, name, adapter):
        if name in self.adapters:
            raise ValueError('DUPLICATE_ADAPTER')
        self.adapters[name] = adapter

    def get(self, name):
        if name not in self.adapters:
            raise ValueError('UNSUPPORTED_ACTOR')
        return self.adapters[name]
