"""读取带有内容摘要的本地业务资料。"""
import hashlib
import json
from pathlib import Path

class DataError(ValueError):
    pass

def decode_rows(content, fields, optional_fields=()):
    """解码登记表行。

    fields 为该表完整登记字段；其中列入 optional_fields 的字段允许物理缺席，
    由快照还原层按各自 schema 默认值补齐。未登记字段仍然一律拒绝。
    """
    fields = list(fields)
    optional = set(optional_fields)
    if optional - set(fields):
        raise DataError('可选字段声明越出登记字段集合')
    rows = []
    seen = set()
    for line_number, line in enumerate(content.decode('utf-8').splitlines(), 1):
        if not line.strip():
            raise DataError(f'第{line_number}行为空')
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise DataError(f'第{line_number}行不是有效JSON') from exc
        if not isinstance(row, dict) or not set(row).issubset(set(fields)):
            raise DataError(f'第{line_number}行字段不符合登记')
        missing = set(fields) - optional - set(row)
        if missing:
            raise DataError(f'第{line_number}行缺少登记字段: {sorted(missing)}')
        key = row.get('id')
        if not isinstance(key, str) or not key or key in seen:
            raise DataError(f'第{line_number}行身份为空或重复')
        seen.add(key)
        rows.append(row)
    return rows

def load(path):
    path = Path(path).resolve()
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise DataError('资料清单不可读取') from exc
    if manifest.get('schema') != 1 or not isinstance(manifest.get('tables'), dict) or not manifest['tables']:
        raise DataError('资料清单版本或表定义无效')
    tables = {}
    for name, entry in manifest['tables'].items():
        asset = (path.parent / entry['file']).resolve()
        if not asset.is_relative_to(path.parent):
            raise DataError(f'{name}的资料文件越过清单目录')
        try:
            content = asset.read_bytes()
        except OSError as exc:
            raise DataError(f'{name}的资料文件不可读取') from exc
        if hashlib.sha256(content).hexdigest() != entry['sha256'] or len(content) != entry['bytes']:
            raise DataError(f'{name}的资料内容摘要不一致')
        rows = decode_rows(content, entry['fields'], entry.get('optional', ()))
        if len(rows) != entry['rows']:
            raise DataError(f'{name}的资料行数不一致')
        tables[name] = rows
    return tables
