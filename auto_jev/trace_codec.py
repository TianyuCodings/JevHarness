"""Readable, reversible sharing of repeated JSON values in complete traces."""
import json


FORMAT = 'auto_jev.lossless-json-dag.v1'
GUIDE = (
    'This is the COMPLETE training feedback, represented without loss. '
    'Resolve every {"$ref": N} using objects[N]. Each object is [kind, value]: '
    '"dict" maps original keys to encoded values, "list" contains encoded values, '
    'and "text" contains the exact original string. Primitive values are literal. '
    'root resolves to the original feedback object with its pipeline records. '
    'Every original JSON dict/list is referenced, so original keys named $ref '
    'remain unambiguous inside dict records. Shared references mean exactly equal '
    'values, not missing data or summaries. Inspect all episodes, decisions and '
    'node records, including all referenced inputs and responses.'
)


def pack_json(value):
    """Intern containers and long text; retain all numbers, fields and records."""
    objects = []
    interned = {}

    def encode(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError('Trace JSON keys must be strings')
            record = ['dict', {key: encode(child) for key, child in sorted(item.items())}]
        elif isinstance(item, list):
            record = ['list', [encode(child) for child in item]]
        elif isinstance(item, str) and len(item) >= 80:
            record = ['text', item]
        else:
            # This also rejects NaN/Infinity and non-JSON objects before dispatch.
            if item is not None and not isinstance(item, (str, bool, int, float)):
                raise ValueError('Only JSON values can be packed')
            json.dumps(item, allow_nan=False)
            return item
        key = json.dumps(record, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False)
        index = interned.get(key)
        if index is None:
            index = len(objects)
            interned[key] = index
            objects.append(record)
        return {'$ref': index}

    root = encode(value)
    return {'encoding': FORMAT, 'guide': GUIDE, 'root': root, 'objects': objects}


def unpack_json(packed):
    """Restore independent JSON values, including original reserved-looking keys."""
    if not isinstance(packed, dict) or packed.get('encoding') != FORMAT:
        raise ValueError('Unknown trace encoding')
    objects = packed['objects']
    if not isinstance(objects, list):
        raise ValueError('Invalid trace object pool')

    def decode(item, ceiling):
        if not isinstance(item, dict):
            if isinstance(item, list):
                raise ValueError('Unreferenced container in packed trace')
            return item
        index = item.get('$ref')
        if (set(item) != {'$ref'} or isinstance(index, bool) or
                not isinstance(index, int) or not 0 <= index < ceiling):
            raise ValueError('Invalid or cyclic trace reference')
        record = objects[index]
        if not isinstance(record, list) or len(record) != 2:
            raise ValueError('Invalid packed trace record')
        kind, value = record
        if kind == 'dict' and isinstance(value, dict):
            return {key: decode(child, index) for key, child in value.items()}
        if kind == 'list' and isinstance(value, list):
            return [decode(child, index) for child in value]
        if kind == 'text' and isinstance(value, str):
            return value
        raise ValueError('Invalid packed trace record kind')

    return decode(packed['root'], len(objects))
