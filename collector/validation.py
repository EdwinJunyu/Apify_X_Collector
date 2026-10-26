"""Small, fail-closed validator for the JSON Schema vocabulary used in M0.

Not a general JSON Schema implementation. Unknown keywords fail schema loading.
Schemas also work with third-party Draft 2020-12 validators.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


class ValidationError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def parse_utc(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', value):
        raise ValidationError('UTC_TIMESTAMP_REQUIRED')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValidationError('INVALID_DATE') from None


def strict_loads(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValidationError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result

    def constant(_value):
        raise ValidationError('NON_FINITE_NUMBER')

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError):
        raise ValidationError('INVALID_JSON') from None


# $defs 只保存具名定义；观测 payload 由存储层显式选取并验证，不实现隐式 $ref。
KEYWORDS = {'$schema', '$id', '$defs', 'title', 'description', 'type', 'properties',
            'required', 'additionalProperties', 'items', 'enum', 'const',
            'minLength', 'maxLength', 'pattern', 'format', 'minimum',
            'maximum', 'maxItems'}


def validate(value, schema):
    for key in schema:
        if key not in KEYWORDS:
            raise ValidationError('UNSUPPORTED_SCHEMA_KEYWORD')
    kind = schema.get('type')
    types = kind if isinstance(kind, list) else [kind]
    good = False
    for item in types:
        if item is None:
            good = True
        elif item == 'null' and value is None:
            good = True
        elif item == 'object' and isinstance(value, dict):
            good = True
        elif item == 'array' and isinstance(value, list):
            good = True
        elif item == 'string' and isinstance(value, str):
            good = True
        elif item == 'boolean' and type(value) is bool:
            good = True
        elif item == 'integer' and type(value) is int:
            good = True
        elif item == 'number' and type(value) in (int, float):
            good = True
    if not good:
        raise ValidationError('FIELD_TYPE')
    if 'const' in schema and value != schema['const']:
        raise ValidationError('FIELD_CONST')
    if 'enum' in schema and value not in schema['enum']:
        raise ValidationError('FIELD_ENUM')
    if isinstance(value, dict):
        fields = schema.get('properties', {})
        for key in schema.get('required', []):
            if key not in value:
                raise ValidationError('MISSING_FIELD')
        for key, child in value.items():
            if key not in fields:
                additional = schema.get('additionalProperties', True)
                if additional is False:
                    raise ValidationError('UNKNOWN_FIELD')
                if isinstance(additional, dict):
                    validate(child, additional)
            else:
                validate(child, fields[key])
    elif isinstance(value, list):
        if len(value) > schema.get('maxItems', 100000):
            raise ValidationError('ARRAY_LIMIT')
        for child in value:
            validate(child, schema.get('items', {}))
    elif isinstance(value, str):
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', 1000000):
            raise ValidationError('STRING_LIMIT')
        if 'pattern' in schema and re.fullmatch(schema['pattern'], value) is None:
            raise ValidationError('FIELD_PATTERN')
        if schema.get('format') == 'date-time':
            parse_utc(value)
    elif type(value) in (int, float):
        if value < schema.get('minimum', float('-inf')) or value > schema.get('maximum', float('inf')):
            raise ValidationError('NUMBER_LIMIT')


def load_schema(name):
    return strict_loads((Path(__file__).parent / 'schemas' / (name + '.json')).read_bytes())


def check(name, value):
    canonical(value)
    validate(value, load_schema(name))
